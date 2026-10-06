"""Serve recorded API responses on 127.0.0.1, without ever contacting the recorded site."""

from __future__ import annotations

import json
import string
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse, urlsplit

from rebrowse import __version__, config
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import (
    canonical_template,
    is_api_request,
    normalize_url,
    parse_body,
    same_site,
)
from rebrowse.reverse.graphql import GraphQLOp, graphql_ops
from rebrowse.safety import redact_body

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE"
EXPOSED_HEADERS = "x-rebrowse-mock, x-rebrowse-route"
JSON = "application/json"
_SCALARS = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class Recording:
    index: int
    path: str
    query: frozenset[tuple[str, str]]
    body: Any
    fields: frozenset[tuple[str, Any]]
    status: int
    content_type: str
    payload: bytes
    missing_body: bool


@dataclass
class Route:
    method: str
    template: str
    operations: tuple[str, ...]
    display: str
    labels: list[str]
    host: str
    recordings: list[Recording] = field(default_factory=list)

    @property
    def name(self) -> str:
        ops = f" [{', '.join(self.labels)}]" if self.labels else ""
        return f"{self.method} {self.host}{self.display}{ops}"


def _pairs(query: str) -> frozenset[tuple[str, str]]:
    return frozenset(parse_qsl(query, keep_blank_values=True))


def _fields(body: Any) -> frozenset[tuple[str, Any]]:
    if not isinstance(body, dict):
        return frozenset()
    return frozenset((key, value) for key, value in body.items() if isinstance(value, _SCALARS))


def _header_value(value: str) -> str:
    return value if value.isascii() and value.isprintable() else ""


def _utf8(content_type: str) -> str:
    mime, _, params = content_type.partition(";")
    return f"{mime.strip()}; charset=utf-8" if "charset" in params.lower() else content_type


def _operations(body: Any, query: str) -> list[GraphQLOp]:
    try:
        return graphql_ops(body, dict(parse_qsl(query)))
    except RecursionError:
        return []


def _path_of(target: str) -> str:
    try:
        return urlparse(target).path
    except ValueError:
        return target.partition("?")[0]


def _servable(req: RawRequest, domain: str) -> bool:
    status = req.response_status
    return (status >= 200 and not 300 <= status < 400
            and same_site(req.url, domain) and is_api_request(req))


def build_routes(capture: CaptureResult) -> list[Route]:
    routes: dict[tuple[str, str, str, tuple[str, ...]], Route] = {}
    for index, req in enumerate(capture.requests):
        if not _servable(req, capture.domain):
            continue
        content_type = _header_value(req.response_headers.get("content-type", ""))
        try:
            text = req.response_body or ""
            payload = redact_body(text, content_type).encode("utf-8")
            body = parse_body(req.request_body)
        except RecursionError:
            continue
        parts = urlparse(req.url)
        host = "" if parts.netloc.lower() == capture.domain.lower() else parts.netloc.lower()
        ops = _operations(body, parts.query)
        display = urlparse(normalize_url(req.url)[0]).path
        template = canonical_template(display)
        tokens = tuple(op.dedup_token() for op in ops)
        key = (req.method, host, template, tokens)
        if key not in routes:
            routes[key] = Route(req.method, template, tokens, display,
                                [op.label() for op in ops], host)
        routes[key].recordings.append(Recording(
            index=index, path=parts.path, query=_pairs(parts.query), body=body,
            fields=_fields(body), status=req.response_status, content_type=_utf8(content_type),
            payload=payload, missing_body=req.response_body is None and bool(content_type)
            and req.response_status != HTTPStatus.NO_CONTENT,
        ))
    return sorted(routes.values(), key=lambda route: route.host != "")


def _fits(template: str, segments: list[str]) -> bool:
    parts = template.split("/")
    return len(parts) == len(segments) and all(
        part == seg or (part == "{}" and seg != "") for part, seg in zip(parts, segments))


def _literals(route: Route) -> int:
    return sum(part != "{}" for part in route.template.split("/"))


def _overlap(recorded: frozenset, requested: frozenset) -> tuple[int, int]:
    return -len(recorded & requested), len(recorded - requested)


def _distance(
    rec: Recording, path: str, query: frozenset[tuple[str, str]], body: Any,
    fields: frozenset[tuple[str, Any]],
) -> tuple:
    return (rec.missing_body, rec.path != path, *_overlap(rec.query, query),
            *_overlap(rec.fields, fields), rec.body != body, not 200 <= rec.status < 300,
            rec.index)


def match(
    routes: list[Route], method: str, target: str, body: bytes,
) -> tuple[Route, Recording, bool] | None:
    parts = urlparse(target)
    try:
        parsed = parse_body(body.decode("utf-8", errors="replace"))
    except RecursionError:
        parsed = None
    tokens = tuple(op.dedup_token() for op in _operations(parsed, parts.query))
    segments = parts.path.split("/")
    fits = [route for route in routes if route.method == method
            and route.operations == tokens and _fits(route.template, segments)]
    if not fits:
        return None
    route = max(fits, key=_literals)
    elsewhere = any(other.host != route.host and other.template == route.template
                    for other in fits)
    query, fields = _pairs(parts.query), _fields(parsed)
    recording = min(route.recordings,
                    key=lambda rec: _distance(rec, parts.path, query, parsed, fields))
    exact = (recording.path, recording.query, recording.body) == (parts.path, query, parsed)
    return route, recording, exact and not elsewhere


def _ordered(routes: list[Route]) -> list[Route]:
    return sorted(routes, key=lambda route: (route.display, route.method, route.host))


def route_table(routes: list[Route]) -> list[dict]:
    table = []
    for route in _ordered(routes):
        row: dict[str, Any] = {"method": route.method, "path": route.display}
        if route.host:
            row["host"] = route.host
        if route.labels:
            row["graphql"] = route.labels
        row["recordings"] = len(route.recordings)
        row["statuses"] = sorted({rec.status for rec in route.recordings})
        table.append(row)
    return table


def _loopback(url: str) -> bool:
    try:
        return urlsplit(url).hostname in LOOPBACK
    except ValueError:
        return False


def _printable(text: str) -> str:
    return quote(text, safe=string.punctuation + " ")


class _Handler(BaseHTTPRequestHandler):
    server: MockServer
    server_version = f"rebrowse-mock/{__version__}"

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[mock] {fmt % args}", file=sys.stderr)

    def _origin(self) -> str | None:
        origin = self.headers.get("origin", "")
        return origin if _loopback(origin) else None

    def _reply(self, status: int, payload: bytes, note: str, headers: dict[str, str]) -> None:
        path = _printable(_path_of(self.path))
        self.log_message("%d %s %s (%s)", status, self.command, path, note)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if origin := self._origin():
            self.send_header("access-control-allow-origin", origin)
            self.send_header("access-control-allow-credentials", "true")
            self.send_header("access-control-expose-headers", EXPOSED_HEADERS)
            self.send_header("vary", "Origin")
        if status != HTTPStatus.NO_CONTENT:
            self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reply_json(
        self, status: int, payload: dict, note: str, headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        self._reply(status, body, note, {"content-type": JSON, **(headers or {})})

    def _refused(self) -> bool:
        if not _loopback(f"//{self.headers.get('host', '')}"):
            self._reply_json(403, {"error": "Host must be localhost or 127.0.0.1"}, "host refused")
            return True
        if self.headers.get("sec-fetch-site") == "cross-site" and not (
                self._origin() or _loopback(self.headers.get("referer", ""))):
            self._reply_json(403, {"error": "Only pages on localhost may call the mock"},
                             "cross-site refused")
            return True
        return False

    def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            try:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return bytes(body)
            if size <= 0:
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(body)
            chunk = self.rfile.read(size)
            self.rfile.readline(1024)
            body += chunk[:max(config.MAX_BODY_SIZE - len(body), 0)]

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("transfer-encoding", "").lower():
            return self._read_chunked()
        try:
            length = max(int(self.headers.get("content-length") or 0), 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, config.MAX_BODY_SIZE))
        remaining = length - len(body)
        while remaining > 0 and (chunk := self.rfile.read(min(remaining, 1 << 16))):
            remaining -= len(chunk)
        return body

    def do_OPTIONS(self) -> None:
        if self._refused():
            return
        if self._origin() is None:
            self._reply_json(403, {"error": "CORS is only answered for loopback origins"},
                             "preflight refused")
            return
        headers = {"access-control-allow-methods": ALLOWED_METHODS}
        if requested := self.headers.get("access-control-request-headers"):
            headers["access-control-allow-headers"] = requested
        self._reply(204, b"", "preflight", headers)

    def _answer(self) -> None:
        body = self._read_body()
        if self._refused():
            return
        routes = self.server.routes
        try:
            found = match(routes, self.command, self.path, body)
        except ValueError:
            self._reply_json(400, {"error": "malformed request target"}, "bad target")
            return
        if found is None:
            self._reply_json(404, {
                "error": "no recorded response",
                "request": f"{self.command} {_path_of(self.path)}",
                "routes": [route.name for route in _ordered(routes)],
            }, "miss", {"x-rebrowse-mock": "miss"})
            return
        route, recording, exact = found
        kind, name = "exact" if exact else "nearest", _printable(route.name)
        headers = {"x-rebrowse-mock": kind, "x-rebrowse-route": name}
        if recording.content_type:
            headers["content-type"] = recording.content_type
        note = f"{kind} {name}" + (", no body recorded" if recording.missing_body else "")
        self._reply(recording.status, recording.payload, note, headers)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer


class MockServer(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, routes: list[Route], port: int = 0) -> None:
        self.routes = routes
        super().__init__(("127.0.0.1", port), _Handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"
