"""Export a skill or a recording as a deterministic OpenAPI 3.1 document."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from http import HTTPStatus
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlparse, urlsplit

from rebrowse.baseline import make_baseline
from rebrowse.capture.har import content_type_of
from rebrowse.drift import Shape, add_body
from rebrowse.mock import Recording, Route, build_routes
from rebrowse.models import CaptureResult, EndpointDescriptor, RawRequest, SkillManifest
from rebrowse.reverse.extractor import (
    PLACEHOLDER_RE,
    STRIP_HEADERS,
    canonical_template,
    is_replay_header,
    normalize_url,
    schema_from_values,
)
from rebrowse.reverse.graphql import request_effect
from rebrowse.safety import FORM, REDACTED, is_secret_name, redact

METHOD_ORDER = ("get", "post", "put", "patch", "delete")
BROWSER_HEADERS = frozenset({
    "content-type", "accept", "accept-language", "referer", "origin",
    "cache-control", "pragma", "priority", "dnt",
})

_NO_CONTENT = frozenset({HTTPStatus.NO_CONTENT, HTTPStatus.RESET_CONTENT, HTTPStatus.NOT_MODIFIED})
_UNKNOWN_MEDIA = "x-unknown"
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
        out["required"] = sorted(schema["required"])
    return out


def _documented_header(name: str) -> bool:
    low = name.lower()
    return not (
        low in BROWSER_HEADERS or low in STRIP_HEADERS or low.startswith("sec-")
        or not is_replay_header(low)
    )


def _headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if _documented_header(k)}


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
        + _optional_parameters("header", (_headers(ep.headers_template) for ep in group))
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


def _content_hash(document: dict) -> str:
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", errors="backslashreplace")).hexdigest()[:12]


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

    info = {"title": skill.name, "description": skill.description,
            "x-rebrowse-skill-id": skill.skill_id}
    document = {
        "openapi": "3.1.0",
        "info": info,
        "servers": [{"url": primary}] if primary else [],
        "paths": paths,
    }
    version = _content_hash(document)
    return {**document, "info": {"title": skill.name, "version": version, **info}}


class _Call(NamedTuple):
    route: Route
    recording: Recording
    request: RawRequest


def _mime(content_type: str) -> str:
    return content_type.partition(";")[0].strip().lower()


def _shape_schema(shape: Shape) -> dict:
    types = sorted(shape.types - {"integer"} if "number" in shape.types else shape.types)
    schema: dict[str, Any] = {"type": types[0] if len(types) == 1 else types}
    if shape.fields:
        schema["properties"] = {
            name: _shape_schema(child) for name, child in sorted(shape.fields.items())}
        if required := [name for name in sorted(shape.fields) if shape.requires(name)]:
            schema["required"] = required
    if shape.entries:
        schema["additionalProperties"] = _shape_schema(shape.entries)
    if shape.items:
        schema["items"] = _shape_schema(shape.items)
    return schema


def _call_origin(call: _Call, site: str) -> str:
    if not call.route.host:
        return site
    return f"{urlsplit(call.request.url).scheme}://{call.route.host}"


def _recorded_path_parameters(path: str, first: RawRequest) -> list[dict]:
    values = normalize_url(first.url)[1]
    params = []
    for name in PLACEHOLDER_RE.findall(path):
        param: dict[str, Any] = {
            "name": name, "in": "path", "required": True, "schema": {"type": "string"},
        }
        if name in values:
            param["example"] = _example(name, values[name])
        params.append(param)
    return params


def _named_parameters(location: str, sources: list[dict[str, str]]) -> list[dict]:
    values: dict[str, str] = {}
    for source in sources:
        for name, value in source.items():
            values.setdefault(name, value)
    return [
        {"name": name, "in": location, "required": all(name in source for source in sources),
         "schema": {"type": "string"}, "example": _example(name, value)}
        for name, value in sorted(values.items())
    ]


def _first_values(query: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        values.setdefault(name, value)
    return values


def _recorded_parameters(path: str, calls: list[_Call]) -> list[dict]:
    queries = [_first_values(urlsplit(call.request.url).query) for call in calls]
    headers = [_headers(call.request.request_headers) for call in calls]
    return (
        _recorded_path_parameters(path, calls[0].request)
        + _named_parameters("query", queries)
        + _named_parameters("header", headers)
    )


def _label(route: Route) -> str:
    return ", ".join(route.labels) or f"{route.method} {route.display}"


def _recorded_media(calls: list[_Call]) -> dict:
    bodies = [(_label(call.route), call.recording.body) for call in calls
              if call.recording.body is not None]
    if not bodies:
        return {}
    shape = Shape()
    examples: dict[str, Any] = {}
    for label, body in bodies:
        shape.add(body)
        examples.setdefault(label, body)
    media: dict[str, Any] = {"schema": _shape_schema(shape)}
    if len(examples) > 1:
        media["examples"] = {label: {"value": body} for label, body in sorted(examples.items())}
    else:
        media["example"] = bodies[0][1]
    return media


def _recorded_body(calls: list[_Call]) -> dict | None:
    by_type: dict[str, list[_Call]] = {}
    for call in calls:
        if call.request.request_body:
            media_type = _mime(content_type_of(call.request.request_headers)) or "application/json"
            by_type.setdefault(media_type, []).append(call)
    if not by_type:
        return None
    return {"content": {
        media_type: _recorded_media(group)
        if _documents_body(media_type) or all(_is_json(c.request.request_body) for c in group)
        else {}
        for media_type, group in sorted(by_type.items())
    }}


def _is_json(text: str | None) -> bool:
    try:
        json.loads(text or "")
    except (ValueError, RecursionError):
        return False
    return True


def _response_key(status: int) -> str:
    return str(status) if 100 <= status <= 599 else "default"


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Recorded response"


def _recorded_response(key: str, shapes: dict[str, Shape]) -> dict:
    description = "Recorded response" if key == "default" else _phrase(int(key))
    response: dict[str, Any] = {"description": description}
    if shapes:
        response["content"] = {
            media_type: {"schema": _shape_schema(shape)} if shape.seen else {}
            for media_type, shape in sorted(shapes.items())
        }
    return response


def _response_media(recording: Recording) -> str:
    """The recorded response's media type; browsers write x-unknown in a HAR when none was sent."""
    media_type = _mime(recording.content_type)
    if recording.status in _NO_CONTENT or media_type == _UNKNOWN_MEDIA:
        return ""
    return media_type


def _recorded_responses(calls: list[_Call]) -> dict:
    statuses: dict[str, dict[str, Shape]] = {}
    for call in calls:
        shapes = statuses.setdefault(_response_key(call.recording.status), {})
        if media_type := _response_media(call.recording):
            add_body(shapes.setdefault(media_type, Shape()), call.request.response_body)
    return {key: _recorded_response(key, statuses[key])
            for key in sorted(statuses, key=lambda key: (key == "default", key))}


def _recorded_effect(calls: list[_Call]) -> str:
    effects = (request_effect(call.request.method, call.request.url, call.recording.body)
               for call in calls)
    return max((effect.value for effect in effects), key=_EFFECT_RANK.get)


def _recorded_operation(path: str, method: str, calls: list[_Call], site: str) -> dict:
    op: dict[str, Any] = {"summary": f"{method.upper()} {path}"}
    if any(call.route.host for call in calls):
        op["servers"] = [{"url": url} for url in sorted({_call_origin(c, site) for c in calls})]
    if params := _recorded_parameters(path, calls):
        op["parameters"] = params
    if method != "get" and (body := _recorded_body(calls)):
        op["requestBody"] = body
    op["responses"] = _recorded_responses(calls)
    op["x-rebrowse-effect"] = _recorded_effect(calls)
    if labels := sorted({label for call in calls for label in call.route.labels}):
        op["x-rebrowse-graphql"] = labels
    return op


def recording_to_openapi(capture: CaptureResult) -> dict:
    """Document what make_baseline keeps, so a recording and its baseline give one document."""
    baseline = make_baseline(capture)
    scheme = urlsplit(baseline.final_url).scheme
    site = f"{scheme if scheme in ('http', 'https') else 'https'}://{baseline.domain}"
    displays: dict[str, str] = {}
    groups: dict[tuple[str, str], list[_Call]] = {}
    for route in build_routes(baseline, redirects=True):
        path = displays.setdefault(route.template, route.display)
        groups.setdefault((path, route.method.lower()), []).extend(
            _Call(route, rec, baseline.requests[rec.index]) for rec in route.recordings)
    keys = sorted(groups, key=lambda k: (k[0], METHOD_ORDER.index(k[1])))
    ids = _unique(_operation_id(method, path) for path, method in keys)

    paths: dict[str, dict] = {}
    for (path, method), operation_id in zip(keys, ids):
        calls = sorted(groups[(path, method)], key=lambda call: call.recording.index)
        paths.setdefault(path, {})[method] = {
            "operationId": operation_id, **_recorded_operation(path, method, calls, site)}

    document = {"openapi": "3.1.0", "info": {"title": f"{baseline.domain} API"},
                "servers": [{"url": site}], "paths": paths}
    return {**document, "info": {**document["info"], "version": _content_hash(document)}}
