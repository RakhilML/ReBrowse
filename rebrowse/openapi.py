"""Export a skill as a deterministic OpenAPI 3.1 document."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from rebrowse.models import EndpointDescriptor, SkillManifest
from rebrowse.reverse.extractor import (
    PLACEHOLDER_RE,
    STRIP_HEADERS,
    canonical_template,
    is_replay_header,
    schema_from_values,
)
from rebrowse.safety import FORM, REDACTED, is_secret_name, redact

METHOD_ORDER = ("get", "post", "put", "patch", "delete")
BROWSER_HEADERS = frozenset({
    "content-type", "accept", "accept-language", "referer", "origin",
    "cache-control", "pragma", "priority", "dnt",
})

_EFFECT_RANK = {"read": 0, "write": 1, "destructive": 2}
_HEALTH_RANK = {"verified": 0, "unverified": 1, "failed": 2}
_WORD = re.compile(r"[A-Za-z0-9]+")


def _origin(ep: EndpointDescriptor) -> str:
    parsed = urlparse(ep.url_template)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname or ''}{port}"


def _path(ep: EndpointDescriptor) -> str:
    return urlparse(ep.url_template).path or "/"


def _summary(ep: EndpointDescriptor) -> str:
    return ep.description or f"{ep.method.value} {_path(ep)}"


def _observed(ep: EndpointDescriptor) -> bool:
    return ep.trigger_url is not None


def _example(name: str, value: Any) -> Any:
    return REDACTED if is_secret_name(name) else redact(value)


def _unique(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for name in names:
        candidate, n = name, 2
        while candidate in seen:
            candidate, n = f"{name}{n}", n + 1
        seen.add(candidate)
        out.append(candidate)
    return out


def _operation_id(method: str, path: str) -> str:
    words = _WORD.findall(path)
    return method + ("".join(w[0].upper() + w[1:] for w in words) if words else "Root")


def _json_schema(schema: dict) -> dict:
    out: dict[str, Any] = {}
    if schema.get("type") not in (None, "unknown"):
        out["type"] = schema["type"]
    if schema.get("properties") is not None:
        out["properties"] = {k: _json_schema(v) for k, v in schema["properties"].items()}
    if schema.get("items") is not None:
        out["items"] = _json_schema(schema["items"])
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out


def _documented_header(name: str) -> bool:
    low = name.lower()
    return not (
        low in BROWSER_HEADERS or low in STRIP_HEADERS or low.startswith("sec-")
        or not is_replay_header(low)
    )


def _headers(ep: EndpointDescriptor) -> dict[str, str]:
    return {k.lower(): v for k, v in ep.headers_template.items() if _documented_header(k)}


def _path_examples(path: str, group: list[EndpointDescriptor]) -> dict[str, str]:
    examples: dict[str, str] = {}
    for ep in group:
        names = dict(zip(PLACEHOLDER_RE.findall(_path(ep)), PLACEHOLDER_RE.findall(path)))
        for name, value in ep.path_params.items():
            if name in names:
                examples.setdefault(names[name], value)
    return examples


def _path_parameters(path: str, group: list[EndpointDescriptor]) -> list[dict]:
    examples = _path_examples(path, group)
    params = []
    for name in dict.fromkeys(PLACEHOLDER_RE.findall(path)):
        param: dict[str, Any] = {
            "name": name, "in": "path", "required": True, "schema": {"type": "string"},
        }
        if name in examples:
            param["example"] = _example(name, examples[name])
        params.append(param)
    return params


def _optional_parameters(location: str, sources: Iterable[dict[str, Any]]) -> list[dict]:
    values: dict[str, Any] = {}
    for source in sources:
        for name, value in source.items():
            values.setdefault(name, value)
    return [
        {"name": name, "in": location, "required": False, "schema": {"type": "string"},
         "example": _example(name, value)}
        for name, value in sorted(values.items())
    ]


def _parameters(path: str, group: list[EndpointDescriptor]) -> list[dict]:
    return (
        _path_parameters(path, group)
        + _optional_parameters("query", (ep.query for ep in group))
        + _optional_parameters("header", (_headers(ep) for ep in group))
    )


def _media_type(ep: EndpointDescriptor) -> str:
    value = next((v for k, v in ep.headers_template.items() if k.lower() == "content-type"), "")
    return value.split(";")[0].strip().lower() or "application/json"


def _documents_body(media_type: str) -> bool:
    return media_type == FORM or media_type.endswith(("/json", "+json"))


def _media(eps: list[EndpointDescriptor], merged: bool) -> dict:
    schema = schema_from_values([ep.body for ep in eps]).model_dump()
    media: dict[str, Any] = {"schema": _json_schema(schema)}
    if merged:
        keys = _unique(_summary(ep) for ep in eps)
        media["examples"] = {key: {"value": redact(ep.body)} for key, ep in zip(keys, eps)}
    else:
        media["example"] = redact(eps[0].body)
    return media


def _request_body(group: list[EndpointDescriptor]) -> dict | None:
    by_type: dict[str, list[EndpointDescriptor]] = {}
    for ep in group:
        if ep.body is not None:
            by_type.setdefault(_media_type(ep), []).append(ep)
    if not by_type:
        return None
    return {"content": {
        media_type: _media(eps, len(group) > 1) if _documents_body(media_type) else {}
        for media_type, eps in sorted(by_type.items())
    }}


def _responses(group: list[EndpointDescriptor]) -> dict:
    schemas: list[dict] = []
    for ep in group:
        if ep.response_schema is not None:
            schema = _json_schema(ep.response_schema.model_dump())
            if schema not in schemas:
                schemas.append(schema)
    if schemas:
        schema = schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
        return {"default": {"description": "Response observed during capture",
                            "content": {"application/json": {"schema": schema}}}}
    if any(_observed(ep) for ep in group):
        return {"default": {"description": "Response body not captured"}}
    return {"default": {"description": "Not observed; route found in a JS bundle"}}


def _merge_group(group: list[EndpointDescriptor]) -> dict:
    return {
        "summary": f"{len(group)} operations",
        "description": "\n".join(f"- {_summary(ep)}" for ep in group),
    }


def _operation(
    path: str, method: str, group: list[EndpointDescriptor], primary: str | None,
) -> dict:
    op: dict[str, Any] = _merge_group(group) if len(group) > 1 else {"summary": _summary(group[0])}
    origins = sorted({_origin(ep) for ep in group})
    if origins != [primary]:
        op["servers"] = [{"url": origin} for origin in origins]
    if params := _parameters(path, group):
        op["parameters"] = params
    if method != "get" and (body := _request_body(group)):
        op["requestBody"] = body
    op["responses"] = _responses(group)
    op["x-rebrowse-effect"] = max((ep.get_effect().value for ep in group), key=_EFFECT_RANK.get)
    op["x-rebrowse-verification"] = max(
        (ep.verification_status.value for ep in group), key=_HEALTH_RANK.get)
    op["x-rebrowse-observed"] = any(_observed(ep) for ep in group)
    if len(group) > 1:
        op["x-rebrowse-operations"] = [
            {"summary": _summary(ep), "effect": ep.get_effect().value,
             "verification": ep.verification_status.value, "observed": _observed(ep)}
            for ep in group
        ]
    return op


def _group_order(ep: EndpointDescriptor) -> tuple[str, str]:
    return _summary(ep), ep.model_dump_json(exclude={"endpoint_id"})


def _primary_origin(endpoints: list[EndpointDescriptor]) -> str | None:
    counts = Counter(_origin(ep) for ep in endpoints)
    return min(counts, key=lambda origin: (-counts[origin], origin), default=None)


def _path_keys(endpoints: list[EndpointDescriptor]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for ep in sorted(endpoints, key=lambda ep: (not _observed(ep), _path(ep))):
        keys.setdefault(canonical_template(_path(ep)), _path(ep))
    return keys


def skill_to_openapi(skill: SkillManifest) -> dict:
    path_keys = _path_keys(skill.endpoints)
    groups: dict[tuple[str, str], list[EndpointDescriptor]] = {}
    for ep in skill.endpoints:
        path = path_keys[canonical_template(_path(ep))]
        groups.setdefault((path, ep.method.value.lower()), []).append(ep)
    keys = sorted(groups, key=lambda k: (k[0], METHOD_ORDER.index(k[1])))
    ids = _unique(_operation_id(method, path) for path, method in keys)
    primary = _primary_origin(skill.endpoints)

    paths: dict[str, dict] = {}
    for (path, method), operation_id in zip(keys, ids):
        group = sorted(groups[(path, method)], key=_group_order)
        paths.setdefault(path, {})[method] = {
            "operationId": operation_id, **_operation(path, method, group, primary)}

    return {
        "openapi": "3.1.0",
        "info": {
            "title": skill.name,
            "version": skill.updated_at,
            "description": skill.description,
            "x-rebrowse-skill-id": skill.skill_id,
        },
        "servers": [{"url": primary}] if primary else [],
        "paths": paths,
    }
