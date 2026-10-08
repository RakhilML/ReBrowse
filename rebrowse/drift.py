"""Report API changes between two recordings of an app that can break its client."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from rebrowse.mock import Route, build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import looks_like_id
from rebrowse.safety import split_xssi

BREAKING, INFO = "breaking", "info"
MAX_DEPTH = 12
TOKEN_LENGTH, TOKEN_DIGIT_RUNS = 16, 3
_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"),
          (list, "array"), (dict, "object"))
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME = re.compile(r"[@$]?[^\W\d][\w.:-]*")
_TOKEN = re.compile(rf"[\w.:-]{{{TOKEN_LENGTH},}}", re.ASCII)
_DIGITS = re.compile(r"\d+")

RouteKey = tuple[str, str, str, tuple[str, ...]]
FieldChange = tuple[str, str, str, list[str] | None, list[str] | None]


def _type(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return "integer"
    return next((name for kind, name in _TYPES if isinstance(value, kind)), "null")


def _is_token(key: str) -> bool:
    return bool(_TOKEN.fullmatch(key)) and len(_DIGITS.findall(key)) >= TOKEN_DIGIT_RUNS


def is_data_key(key: str) -> bool:
    """True for a key that holds a recorded value, such as an id, email, date or token."""
    return not _NAME.fullmatch(key) or looks_like_id(key) or _is_token(key)


@dataclass
class Shape:
    types: set[str] = field(default_factory=set)
    objects: int = 0
    seen: int = 0
    fields: dict[str, Shape] = field(default_factory=dict)
    entries: Shape | None = None
    items: Shape | None = None

    def add(self, value: Any, depth: int = 0) -> None:
        self.seen += 1
        self.types.add(_type(value))
        if depth >= MAX_DEPTH:
            return
        if isinstance(value, dict):
            self.objects += 1
            for name, child in value.items():
                self._member(name).add(child, depth + 1)
        elif isinstance(value, list) and value:
            items = self.items = self.items or Shape()
            for item in value:
                items.add(item, depth + 1)

    def _member(self, name: str) -> Shape:
        if is_data_key(name):
            self.entries = self.entries or Shape()
            return self.entries
        return self.fields.setdefault(name, Shape())

    def requires(self, name: str) -> bool:
        child = self.fields.get(name)
        return child is not None and child.seen == self.objects


@dataclass
class Facts:
    route: Route
    statuses: set[int]
    media: set[str] = field(default_factory=set)
    shape: Shape = field(default_factory=Shape)
    empty: bool = False


def add_body(shape: Shape, text: str | None) -> None:
    """Add the JSON in TEXT, behind any XSSI guard, to SHAPE; anything else is skipped."""
    try:
        body = json.loads(split_xssi(text or "")[1])
    except (ValueError, RecursionError):
        return
    shape.add(body)


def _facts(route: Route, requests: list[RawRequest]) -> Facts:
    facts = Facts(route, {rec.status for rec in route.recordings})
    for rec in route.recordings:
        if not 200 <= rec.status < 300:
            continue
        if media := rec.content_type.partition(";")[0].strip().lower():
            facts.media.add(media)
        text = requests[rec.index].response_body
        facts.empty |= rec.status == 204 or text == ""
        add_body(facts.shape, text)
    return facts


def _key(route: Route) -> RouteKey:
    return route.method, route.host, route.template, route.operations


def route_facts(capture: CaptureResult) -> dict[RouteKey, Facts]:
    routes = build_routes(capture, redirects=True)
    return {_key(route): _facts(route, capture.requests) for route in routes}


def _types(node: Shape | None) -> list[str] | None:
    return sorted(node.types) if node else None


def field_path(parent: str, name: str) -> str:
    if _IDENTIFIER.fullmatch(name):
        return f"{parent}.{name}"
    return f"{parent}[{json.dumps(name, ensure_ascii=False)}]"


def _compare_shapes(base: Shape, head: Shape, path: str) -> Iterator[FieldChange]:
    known = (base.types | {"integer"}) if "number" in base.types else base.types
    if added := head.types - known:
        widened = added == {"number"} and "integer" in base.types
        yield INFO if widened else BREAKING, "field_type", path, _types(base), _types(head)
    if base.objects and head.objects:
        for name in sorted(base.fields.keys() | head.fields.keys()):
            yield from _compare_field(base, head, name, field_path(path, name))
    if base.entries and head.entries:
        yield from _compare_shapes(base.entries, head.entries, path + ".*")
    if base.items and head.items:
        yield from _compare_shapes(base.items, head.items, path + "[]")


def _compare_field(base: Shape, head: Shape, name: str, path: str) -> Iterator[FieldChange]:
    before, after = base.fields.get(name), head.fields.get(name)
    if before and after:
        if base.requires(name) and not head.requires(name):
            yield BREAKING, "field_optional", path, _types(before), _types(after)
        yield from _compare_shapes(before, after, path)
    elif before:
        severity = BREAKING if base.requires(name) else INFO
        yield severity, "field_removed", path, _types(before), None
    else:
        yield INFO, "field_added", path, None, _types(after)


def _change(severity: str, kind: str, route: str, base: Any, head: Any,
            path: str | None = None) -> dict:
    change = {"severity": severity, "kind": kind, "route": route}
    if path is not None:
        change["field"] = path
    return {**change, "base": base, "head": head}


def answers(statuses: set[int]) -> bool:
    return any(200 <= status < 300 or status == 304 for status in statuses)


def status_breaks(before: set[int], after: set[int]) -> bool:
    if not answers(before):
        return False
    return not answers(after) or any(status >= 500 for status in after - before)


def _compare_route(before: Facts, after: Facts) -> Iterator[dict]:
    name = before.route.name
    statuses = sorted(before.statuses), sorted(after.statuses)
    if status_breaks(before.statuses, after.statuses):
        yield _change(BREAKING, "status", name, *statuses)
    elif before.statuses != after.statuses:
        yield _change(INFO, "status", name, *statuses)
    if before.media and after.media - before.media:
        yield _change(BREAKING, "content_type", name, sorted(before.media), sorted(after.media))
    if before.shape.seen and not after.shape.seen and after.empty:
        yield _change(BREAKING, "body_empty", name, sorted(before.media), sorted(after.media))
    if before.shape.seen and after.shape.seen:
        for severity, kind, path, was, now in _compare_shapes(before.shape, after.shape, "$"):
            yield _change(severity, kind, name, was, now, path)


def _order(facts: Facts) -> tuple:
    route = facts.route
    return route.name, route.method, route.host, route.operations, route.template


def compare_facts(base: dict[RouteKey, Facts], head: dict[RouteKey, Facts]) -> list[dict]:
    pairs = [(base.get(key), head.get(key)) for key in base.keys() | head.keys()]
    changes: list[dict] = []
    for old, new in sorted(pairs, key=lambda pair: _order(pair[0] or pair[1])):
        if old and new:
            changes.extend(_compare_route(old, new))
        elif old:
            changes.append(_change(INFO, "route_missing", old.route.name,
                                   sorted(old.statuses), None))
        else:
            changes.append(_change(INFO, "route_added", new.route.name,
                                   None, sorted(new.statuses)))
    return changes


def compare(base: CaptureResult, head: CaptureResult) -> list[dict]:
    return compare_facts(route_facts(base), route_facts(head))
