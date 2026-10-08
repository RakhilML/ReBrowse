"""Turn a recording into one safe to commit: no credentials, no response values but sent ids."""

from __future__ import annotations

import json
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rebrowse import drift, follow
from rebrowse.capture.har import (
    clean_url,
    content_type_of,
    credential_in_path,
    scrub_url,
    sendable_headers,
)
from rebrowse.capture.store import write_atomic
from rebrowse.mock import build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import parse_body
from rebrowse.reverse.graphql import graphql_ops, request_effect
from rebrowse.safety import ACTION_KEYS, Effect, blank_document, redact, redact_body, split_xssi

_OPERATION_KEYS = {"operationName": str, "query": str, "extensions": dict}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _distinct(values: Iterable[Any]) -> list[Any]:
    unique = {_canonical(value): value for value in values}
    return [unique[key] for key in sorted(unique)]


def _object(value: dict, depth: int, linked: AbstractSet[str]) -> dict:
    members = {key: _placeholder(child, depth + 1, linked, key) for key, child in value.items()
               if not drift.is_data_key(key)}
    entries = _distinct(_placeholder(child, depth + 1) for key, child in value.items()
                        if drift.is_data_key(key))
    members.update((str(index), entry) for index, entry in enumerate(entries))
    return dict(sorted(members.items()))


def _placeholder(value: Any, depth: int = 0, linked: AbstractSet[str] = frozenset(),
                 key: str | None = None) -> Any:
    """A value that diff reads as it reads VALUE, keeping only the LINKED ids reads send."""
    if isinstance(value, (dict, list)) and depth >= drift.MAX_DEPTH:
        return type(value)()
    if isinstance(value, dict):
        return _object(value, depth, linked)
    if isinstance(value, list):
        return _distinct(_placeholder(item, depth + 1, linked, key) for item in value)
    if follow.id_leaf(key, value) and str(value) in linked:
        return value
    if isinstance(value, bool):
        return False
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return 0
    if isinstance(value, float):
        return 0.5
    return "" if isinstance(value, str) else None


def _write_url(url: str) -> str:
    """URL with the query values of a write emptied, but those that name or classify it."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return url
    ops = graphql_ops(None, dict(pairs))
    named = bool(ops) and bool(ops[0].name or ops[0].hash)

    def kept(name: str, value: str) -> str:
        if name in ("operationName", "extensions") or name.lower() in ACTION_KEYS:
            return value
        if name == "query" and ops:
            return blank_document(value) if named else value
        return ""

    query = urlencode([(name, kept(name, value)) for name, value in pairs])
    return urlunsplit(parts._replace(query=query))


def _page_url(url: str) -> str:
    page = (clean_url(url) or "").partition("?")[0]
    if not credential_in_path(page):
        return page
    parts = urlsplit(page)
    return f"{parts.scheme}://{parts.netloc}/"


def _write_item(item: Any) -> Any:
    shape = _placeholder(item)
    ops = graphql_ops(item) if isinstance(item, dict) else []
    if not ops:
        return shape
    kept = redact({key: item[key] for key, kind in _OPERATION_KEYS.items()
                   if isinstance(item.get(key), kind)})
    if "query" in kept and (ops[0].name or ops[0].hash):
        kept["query"] = blank_document(kept["query"])
    return dict(sorted({**shape, **kept}.items()))


def _write_form(text: str) -> str | None:
    pairs = dict(parse_qsl(text, keep_blank_values=True))
    ops = graphql_ops(pairs)
    if not ops:
        return None
    kept = redact({key: pairs[key] for key in _OPERATION_KEYS if key in pairs})
    if "query" in kept and (ops[0].name or ops[0].hash):
        kept["query"] = blank_document(kept["query"])
    return urlencode(sorted(kept.items()))


def _write_body(text: str) -> str | None:
    try:
        body = json.loads(text)
    except ValueError:
        return _write_form(text)
    if isinstance(body, list) and graphql_ops(body):
        return _compact([_write_item(item) for item in body])
    return _compact(_write_item(body))


def _request_body(req: RawRequest, read: bool) -> str | None:
    if req.request_body is None:
        return None
    if read:
        return redact_body(req.request_body, content_type_of(req.request_headers))
    return _write_body(req.request_body)


def _response_body(text: str | None, linked: AbstractSet[str]) -> str | None:
    if not text:
        return text
    guard, rest = split_xssi(text)
    try:
        body = json.loads(rest)
    except (ValueError, RecursionError):
        return None
    return guard + _compact(_placeholder(body, linked=linked))


def _scrub(req: RawRequest) -> RawRequest | None:
    url = scrub_url(req.url)
    if url is None:
        return None
    try:
        read = request_effect(req.method, url, parse_body(req.request_body)) is Effect.READ
        request_body = _request_body(req, read)
    except RecursionError:
        return None
    if not read:
        url = _write_url(url)
    content_type = req.response_headers.get("content-type", "")
    printable = content_type.isascii() and content_type.isprintable()
    return RawRequest(
        url=url,
        method=req.method,
        request_headers=dict(sorted(sendable_headers(req.request_headers).items())),
        request_body=request_body,
        response_status=req.response_status,
        response_headers={"content-type": content_type} if content_type and printable else {},
    )


def _routed_indexes(capture: CaptureResult) -> list[int]:
    """The requests diff and contract read: same-site API calls with redirects and 304s."""
    return [rec.index for route in build_routes(capture, redirects=True)
            for rec in route.recordings]


def _routed(capture: CaptureResult) -> list[RawRequest]:
    return [capture.requests[index] for index in _routed_indexes(capture)]


def _read(req: RawRequest) -> bool:
    return request_effect(req.method, req.url, parse_body(req.request_body)) is Effect.READ


def _key(req: RawRequest) -> str:
    return _canonical(req.model_dump(exclude={"timestamp"}))


def _order(req: RawRequest) -> tuple:
    return (req.method, req.url, req.request_body or "", req.response_status,
            req.response_headers.get("content-type", ""), req.response_body or "", _key(req))


def make_baseline(capture: CaptureResult) -> CaptureResult:
    """The calls diff, contract and mock judge, without credentials or values but sent ids."""
    pairs = [(req, clean) for req in _routed(capture) if (clean := _scrub(req)) is not None]
    site = CaptureResult(domain=capture.domain, final_url=_page_url(capture.final_url),
                         requests=[clean for _, clean in pairs])
    routed = [pairs[index] for index in _routed_indexes(site)]
    kept = [(req, clean, _read(clean)) for req, clean in routed]
    linked = follow.linked_values(clean for _, clean, read in kept if read)
    answered = [clean.model_copy(update={"response_body": _response_body(
        req.response_body, linked if read else frozenset())}) for req, clean, read in kept]
    unique = {_key(req): req for req in answered}
    return site.model_copy(update={"requests": sorted(unique.values(), key=_order)})


def baseline_bytes(capture: CaptureResult) -> bytes:
    """Indented UTF-8 JSON without timestamps; a lone surrogate is written as its \\u escape."""
    requests = [req.model_dump(mode="json", exclude={"timestamp"}) for req in capture.requests]
    document = {"domain": capture.domain, "final_url": capture.final_url, "requests": requests}
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8", errors="backslashreplace")


def write_baseline(path: Path, data: bytes) -> None:
    write_atomic(path, data)
