"""Trace each id a recorded read sends to the earlier read whose answer held it."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import cache
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, quote_plus, urlparse, urlunparse

from rebrowse import drift
from rebrowse.models import RawRequest
from rebrowse.reverse.extractor import PLACEHOLDER_RE, looks_like_id, normalize_url
from rebrowse.safety import REDACTED, is_secret_name, split_xssi

PATH, QUERY, BODY = "path", "query", "body"
MAX_VALUE_LENGTH = 200
_ID_WORDS = frozenset({"id", "ids", "uuid", "guid"})
_ID_SUFFIX = re.compile(r"(?:[a-z0-9](?:Id|ID)s?|(?i:_ids?|-id))\Z")
_ID_END = re.compile(r"(?:ids?|uuid|guid)\d*\Z")
_SEPARATORS = re.compile(r"[^a-z0-9]")
_SEGMENT = re.compile(r"[A-Za-z0-9_,-]+")
_MISSING = object()

Key = str | int


class Position(NamedTuple):
    where: str
    name: str
    value: str | int
    keys: tuple[Key, ...]


class Leaf(NamedTuple):
    field: str
    keys: tuple[Key, ...]
    value: str | int


class Read(NamedTuple):
    route: str
    sent: RawRequest
    answers: list[RawRequest]


class Link(NamedTuple):
    dependent: int
    position: Position
    source: int
    field: str
    keys: tuple[Key, ...]


def id_name(name: str) -> bool:
    return ((name.lower() in _ID_WORDS or bool(_ID_SUFFIX.search(name)))
            and not is_secret_name(name))


def id_value(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def id_leaf(key: str | None, value: Any) -> bool:
    """Whether VALUE, held under KEY, can be an id another read sends."""
    return key is not None and id_value(value) and id_name(key)


def _linkable(value: str | int) -> bool:
    return str(value) not in ("", REDACTED)


def _leaves(value: Any, field: str = "$", keys: tuple[Key, ...] = (), key: str | None = None,
            depth: int = 0) -> Iterator[Leaf]:
    """Each str or int under an id-named key, never under a data key, at most MAX_DEPTH deep."""
    if isinstance(value, dict):
        if depth < drift.MAX_DEPTH:
            for name, child in value.items():
                if not drift.is_data_key(name):
                    yield from _leaves(child, drift.field_path(field, name), (*keys, name),
                                       name, depth + 1)
    elif isinstance(value, list):
        if depth < drift.MAX_DEPTH:
            for index, item in enumerate(value):
                yield from _leaves(item, f"{field}[{index}]", (*keys, index), key, depth + 1)
    elif id_leaf(key, value):
        yield Leaf(field, keys, value)


def json_answer(text: str | None) -> Any:
    """The JSON in TEXT behind any XSSI guard, or None."""
    try:
        return json.loads(split_xssi(text or "")[1])
    except (ValueError, RecursionError):
        return None


def _path_positions(url: str) -> list[Position]:
    segments = urlparse(url).path.split("/")
    names = urlparse(normalize_url(url)[0]).path.split("/")
    if len(segments) != len(names):
        return []
    return [Position(PATH, name[1:-1], segment, (index,))
            for index, (segment, name) in enumerate(zip(segments, names))
            if segment != name and PLACEHOLDER_RE.fullmatch(name)]


def _query_positions(url: str) -> list[Position]:
    pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
    return [Position(QUERY, name, value, (index,)) for index, (name, value) in enumerate(pairs)
            if id_name(name) and _linkable(value)]


def _body_positions(req: RawRequest) -> list[Position]:
    if req.method == "GET" or not req.request_body:
        return []
    try:
        body = json.loads(req.request_body)
    except (ValueError, RecursionError):
        return []
    return [Position(BODY, leaf.field, leaf.value, leaf.keys) for leaf in _leaves(body)
            if _linkable(leaf.value)]


def positions(req: RawRequest) -> list[Position]:
    """The ids REQ sends: path ids, id-named query values and id-named JSON body fields."""
    return [*_path_positions(req.url), *_query_positions(req.url), *_body_positions(req)]


def linked_values(requests: Iterable[RawRequest]) -> set[str]:
    return {str(position.value) for req in requests for position in positions(req)}


def _held(answers: list[RawRequest]) -> dict[str, list[Leaf]]:
    held: dict[str, list[Leaf]] = {}
    for answer in answers:
        for leaf in _leaves(json_answer(answer.response_body)):
            held.setdefault(str(leaf.value), []).append(leaf)
    return held


@cache
def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


@cache
def _word(name: str) -> str:
    """NAME lowercased, singular and without separators: Users and user give user."""
    return _singular(_SEPARATORS.sub("", name.lower()))


@cache
def _subject(name: str) -> str:
    """What an id named NAME identifies: userId, user_id and users_id2 give user, id gives ''."""
    return _singular(_ID_END.sub("", _SEPARATORS.sub("", name.lower())))


def _names(keys: tuple[Key, ...]) -> list[str]:
    return [key for key in keys if isinstance(key, str)]


def _wanted(position: Position) -> str:
    return _subject(_names(position.keys)[-1] if position.where == BODY else position.name)


def _about(req: RawRequest) -> str:
    """The last literal segment of REQ's path template, which names what it answers with."""
    segments = urlparse(normalize_url(req.url)[0]).path.split("/")
    return next((_word(segment) for segment in reversed(segments)
                 if segment and not PLACEHOLDER_RE.fullmatch(segment)), "")


def _plain_id(subject: str, leaf: Leaf, about: str) -> bool:
    """Whether LEAF is a bare id of SUBJECT: $.products[1].id, or $.id from /api/products/42."""
    *outer, key = _names(leaf.keys)
    return bool(subject) and not _subject(key) and subject in {about, *map(_word, outer[-1:])}


def _naming(subject: str, leaf: Leaf) -> int:
    """0 when LEAF's key names SUBJECT, as userId does user, 1 when it names nothing, else 2."""
    named = _subject(_names(leaf.keys)[-1])
    if not named:
        return 1
    return 0 if named == subject else 2


def _reaches(sources: Mapping[int, set[int]], start: int, goal: int) -> bool:
    seen: set[int] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(sources.get(node, ()))
    return False


def _choose(candidates: list[tuple], sources: Mapping[int, set[int]],
            dependent: int) -> tuple | None:
    """The best candidate that closes no cycle, or None when another route's ties with it."""
    usable = (c for c in candidates
              if c[5] != dependent and not _reaches(sources, c[5], dependent))
    best = next(usable, None)
    if best is None:
        return None
    for other in usable:
        if other[:3] != best[:3]:
            break
        if other[3] != best[3]:
            return None
    return best


def _send_order(count: int, sources: Mapping[int, set[int]]) -> list[int]:
    order: list[int] = []
    done: set[int] = set()
    while len(order) < count:
        ready = next(index for index in range(count)
                     if index not in done and sources.get(index, set()) <= done)
        order.append(ready)
        done.add(ready)
    return order


def link(reads: Sequence[Read]) -> tuple[list[Link], list[int]]:
    """The read whose recorded answer held each id of READS, and an order sending it first."""
    wanted = [positions(read.sent) for read in reads]
    held = [_held(read.answers) for read in reads]
    about = [_about(read.sent) for read in reads]
    sources: dict[int, set[int]] = {}
    links: list[Link] = []
    ranked: dict[tuple[str, str], list[tuple]] = {}
    for dependent, found in enumerate(wanted):
        for position in found:
            subject, value = _wanted(position), str(position.value)
            if (subject, value) not in ranked:
                ranked[subject, value] = sorted(
                    (not _plain_id(subject, leaf, about[source]), len(wanted[source]),
                     _naming(subject, leaf), reads[source].route, leaf.field, source, leaf.keys)
                    for source in range(len(reads)) for leaf in held[source].get(value, ()))
            if chosen := _choose(ranked[subject, value], sources, dependent):
                *_, field, source, keys = chosen
                links.append(Link(dependent, position, source, field, keys))
                sources.setdefault(dependent, set()).add(source)
    return links, _send_order(len(reads), sources)


def _at(value: Any, keys: tuple[Key, ...]) -> Any:
    for key in keys:
        if isinstance(key, int):
            if not isinstance(value, list) or key >= len(value):
                return _MISSING
        elif not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _sendable(where: str, value: Any) -> bool:
    if not id_value(value):
        return False
    text = str(value)
    if not 0 < len(text) <= MAX_VALUE_LENGTH or not text.isprintable():
        return False
    return where != PATH or _path_id(text)


def _path_id(text: str) -> bool:
    """One segment that templates as an id, or a number too short to, such as a seeded 1."""
    return bool(_SEGMENT.fullmatch(text)) and (text.isdigit() or looks_like_id(text))


def resolve(link: Link, answer: Any) -> str | int | None:
    """The id ANSWER holds where LINK's field was, or in its first items; None if unsafe."""
    value = _at(answer, link.keys)
    if value is _MISSING:
        value = _at(answer, tuple(0 if isinstance(key, int) else key for key in link.keys))
    return value if _sendable(link.position.where, value) else None


def _set(body: Any, keys: tuple[Key, ...], value: str | int) -> None:
    parent = body
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value


def rewrite(req: RawRequest, values: Mapping[Position, str | int]) -> RawRequest:
    """REQ sending each of VALUES at its position; a body id stays an int only if both were."""
    kinds = {position.where for position in values}
    parts = urlparse(req.url)
    segments = parts.path.split("/")
    raw = [part for part in parts.query.split("&") if part]
    document = json.loads(req.request_body or "") if BODY in kinds else None
    for position, value in values.items():
        if position.where == PATH:
            segments[position.keys[0]] = str(value)
        elif position.where == QUERY:
            name = raw[position.keys[0]].partition("=")[0]
            raw[position.keys[0]] = f"{name}={quote_plus(str(value))}"
        else:
            ints = isinstance(position.value, int) and isinstance(value, int)
            _set(document, position.keys, value if ints else str(value))
    query = "&".join(raw) if QUERY in kinds else parts.query
    body = req.request_body
    if BODY in kinds:
        body = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    url = urlunparse(parts._replace(path="/".join(segments), query=query))
    return req.model_copy(update={"url": url, "request_body": body})
