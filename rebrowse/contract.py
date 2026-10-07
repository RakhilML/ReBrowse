"""Replay recorded reads against a server and report the changes that break their client."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from http import HTTPStatus
from itertools import islice
from typing import NamedTuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx

from rebrowse import config, drift, net
from rebrowse.auth.vault import get_api_key
from rebrowse.capture.har import clean_url, credential_value, replayable_header
from rebrowse.execution import executor
from rebrowse.mock import LOOPBACK, Recording, Route, build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import is_api_request
from rebrowse.reverse.graphql import graphql_ops
from rebrowse.safety import Effect, classify_effect, redact_body

MAX_REPLAYS_PER_ROUTE = 3
DEADLINE_FACTOR = 3
NO_RESPONSE, BLOCKED = "no_response", "blocked"
NON_API = "answered with a non-API response"
UNREPLAYABLE = "unreplayable"
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_NOT_SENT = frozenset({
    "origin", "referer", "content-encoding",
    "if-none-match", "if-modified-since", "if-match", "if-unmodified-since", "if-range",
    "x-http-method-override", "x-http-method", "x-method-override",
})
_RECEIVED = ("content-encoding", "content-length", "transfer-encoding")


class Replay(NamedTuple):
    route: Route
    recorded: RawRequest
    request: httpx.Request


class Failure(NamedTuple):
    route: str
    kind: str
    error: str


Answer = tuple[Replay, RawRequest]


def parse_target(text: str) -> str:
    """Normalize an http(s) scheme://host[:port]; raises ValueError for anything else."""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"'{text}' is not an origin such as http://localhost:8000")
    if "@" in parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"'{text}' must be scheme://host[:port], without user info, path, "
                         "query or fragment")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = "" if parts.port is None else f":{parts.port}"
    return f"{parts.scheme}://{host}{port}"


def _effect(req: RawRequest, rec: Recording) -> Effect:
    ops = graphql_ops(rec.body, dict(parse_qsl(urlsplit(req.url).query)))
    bodies = [op.body if op.body is not None else {"operationName": op.name, "query": op.query}
              for op in ops]
    effects = {classify_effect(req.method, req.url, body) for body in [rec.body, *bodies]}
    return max(effects, key=list(Effect).index)


def _credential_in_path(url: str) -> bool:
    return any(credential_value(unquote(segment)) for segment in urlsplit(url).path.split("/"))


def _skip_reason(route: Route, req: RawRequest, rec: Recording) -> str | None:
    if route.host:
        return "other host"
    effect = _effect(req, rec)
    if effect is not Effect.READ:
        return effect.value
    if rec.status == HTTPStatus.NOT_MODIFIED:
        return "not modified"
    if "event-stream" in rec.content_type.lower():
        return "stream"
    return "credential in path" if _credential_in_path(req.url) else None


def _candidates(route: Route, requests: list[RawRequest]) -> list[RawRequest]:
    unique: dict[tuple[str, str, str | None], RawRequest] = {}
    for rec in route.recordings:
        req = requests[rec.index]
        if _skip_reason(route, req, rec) is None:
            unique.setdefault((req.method, req.url, req.request_body), req)
    return list(unique.values())


def _build(req: RawRequest, target: str) -> httpx.Request | None:
    try:
        return replay_request(req, target)
    except (httpx.InvalidURL, ValueError):
        return None


def _picks(route: Route, candidates: list[RawRequest], target: str) -> list[Replay]:
    built = (Replay(route, req, request) for req in candidates
             if (request := _build(req, target)) is not None)
    return list(islice(built, MAX_REPLAYS_PER_ROUTE))


def _skipped(route: Route, requests: list[RawRequest], candidates: list[RawRequest]) -> dict:
    first = route.recordings[0]
    reason = UNREPLAYABLE if candidates else _skip_reason(route, requests[first.index], first)
    return {"route": route.name, "reason": reason}


def plan(capture: CaptureResult, target: str) -> tuple[list[Replay], list[dict]]:
    replays: list[Replay] = []
    skipped: list[dict] = []
    for route in build_routes(capture, redirects=True):
        candidates = _candidates(route, capture.requests)
        if picks := _picks(route, candidates, target):
            replays.extend(picks)
        else:
            skipped.append(_skipped(route, capture.requests, candidates))
    return replays, skipped


def _content_type(headers: dict[str, str]) -> str:
    return next((value for name, value in headers.items() if name.lower() == "content-type"), "")


def _sendable(name: str, value: str) -> bool:
    return (bool(_HEADER_NAME.fullmatch(name)) and value.isascii() and value.isprintable()
            and replayable_header(name) and name.lower() not in _NOT_SENT
            and not credential_value(value))


def _headers(recorded: dict[str, str]) -> dict[str, str]:
    stripped = {name: value.strip() for name, value in recorded.items()}
    headers = {name: value for name, value in stripped.items() if _sendable(name, value)}
    headers["User-Agent"] = config.REBROWSE_UA
    return headers


def _query(recorded: str, key_query: dict[str, str]) -> str:
    if key_query:
        pairs = parse_qsl(recorded, keep_blank_values=True)
        recorded = urlencode([(name, value) for name, value in pairs if name not in key_query])
    return "&".join(part for part in (recorded, urlencode(key_query)) if part)


def _target_key(target: str) -> dict | None:
    return get_api_key(urlsplit(target).hostname or "")


def replay_request(req: RawRequest, target: str) -> httpx.Request:
    """Raises httpx.InvalidURL or ValueError for a recording that cannot be sent."""
    recorded = clean_url(req.url)
    if recorded is None:
        raise ValueError("the recorded URL is not an http(s) URL")
    parts = urlsplit(recorded)
    headers = _headers(req.request_headers)
    key_query: dict[str, str] = {}
    if key := _target_key(target):
        executor.add_api_key(key, headers, key_query)
    query = _query(parts.query, key_query)
    url = f"{target}{parts.path or '/'}" + (f"?{query}" if query else "")
    body = None
    if req.method != "GET" and req.request_body is not None:
        body = redact_body(req.request_body, _content_type(req.request_headers))
    return httpx.Request(req.method, url, headers=headers, content=body)


def _live(req: RawRequest, resp: httpx.Response) -> RawRequest:
    content_type = resp.headers.get("content-type", "")
    textual = any(kind in content_type.lower() for kind in ("json", "text"))
    keep = textual and len(resp.content) <= config.MAX_BODY_SIZE
    return req.model_copy(update={
        "response_status": resp.status_code,
        "response_headers": {"content-type": content_type} if content_type else {},
        "response_body": resp.text if keep else None,
    })


def _judge(replay: Replay, resp: httpx.Response) -> RawRequest | Failure:
    if reason := executor.blocked_reason(resp):
        return Failure(replay.route.name, BLOCKED, reason)
    live = _live(replay.recorded, resp)
    return live if is_api_request(live) else Failure(replay.route.name, NO_RESPONSE, NON_API)


async def _fetch(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
    """Read at most MAX_BODY_SIZE + 1 bytes within one deadline, so streams cannot hang a run."""
    async with asyncio.timeout(executor.TIMEOUT_S * DEADLINE_FACTOR):
        resp = await client.send(request, stream=True)
        body = bytearray()
        try:
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > config.MAX_BODY_SIZE:
                    break
        finally:
            await resp.aclose()
    headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _RECEIVED]
    return httpx.Response(resp.status_code, headers=headers, content=bytes(body),
                          request=request)


async def _replay_all(
    replays: list[Replay], target: str,
) -> tuple[list[Answer], list[Failure], int]:
    host = urlsplit(target).hostname or ""
    answers: list[Answer] = []
    failed: list[Failure] = []
    answered = 0
    async with net.client(executor.TIMEOUT_S, follow_redirects=False) as client:
        for replay in replays:
            if host not in LOOPBACK:
                await executor.pace(host)
            try:
                resp = await _fetch(client, replay.request)
            except httpx.HTTPError as e:
                failed.append(Failure(replay.route.name, NO_RESPONSE, f"{type(e).__name__}: {e}"))
                continue
            except TimeoutError:
                deadline = executor.TIMEOUT_S * DEADLINE_FACTOR
                failed.append(Failure(replay.route.name, NO_RESPONSE,
                                      f"TimeoutError: no complete answer within {deadline:g}s"))
                continue
            answered += 1
            outcome = _judge(replay, resp)
            if isinstance(outcome, Failure):
                failed.append(outcome)
            else:
                answers.append((replay, outcome))
    return answers, failed, answered


def _failures(failed: list[Failure]) -> list[dict]:
    counts = Counter(failure.route for failure in failed)
    first: dict[str, Failure] = {}
    for failure in failed:
        first.setdefault(failure.route, failure)
    return [{"severity": drift.BREAKING, "kind": f.kind, "route": f.route, "error": f.error,
             "failed": counts[f.route]} for f in first.values()]


def _regressions(answers: list[Answer]) -> list[dict]:
    statuses: dict[str, tuple[set[int], set[int]]] = {}
    for replay, live in answers:
        before, after = {replay.recorded.response_status}, {live.response_status}
        new_error = live.response_status >= 500 > replay.recorded.response_status
        if new_error or drift.status_breaks(before, after):
            was, now = statuses.setdefault(replay.route.name, (set(), set()))
            was.update(before)
            now.update(after)
    return [{"severity": drift.BREAKING, "kind": "status", "route": route, "base": sorted(was),
             "head": sorted(now)} for route, (was, now) in statuses.items()]


def _side(capture: CaptureResult, requests: list[RawRequest]) -> CaptureResult:
    return CaptureResult(domain=capture.domain, final_url=capture.final_url, requests=requests)


def _changes(capture: CaptureResult, answers: list[Answer], failed: list[Failure]) -> list[dict]:
    base = _side(capture, [replay.recorded for replay, _ in answers])
    head = _side(capture, [live for _, live in answers])
    regressions = _regressions(answers)
    regressed = {change["route"] for change in regressions}
    failures = _failures(failed)
    reported = {failure["route"] for failure in failures}
    answered = Counter(replay.route.name for replay, _ in answers)
    drifted = []
    for change in drift.compare(base, head):
        if change["kind"] == "route_missing":
            if change["route"] not in reported:
                failures.append({"severity": drift.BREAKING, "kind": NO_RESPONSE,
                                 "route": change["route"], "error": NON_API,
                                 "failed": answered[change["route"]]})
        elif not (change["kind"] == "status" and change["route"] in regressed):
            drifted.append(change)
    return sorted(regressions + drifted + failures, key=lambda change: change["route"])


async def run_contract(capture: CaptureResult, target: str) -> dict:
    replays, skipped = plan(capture, target)
    if not replays:
        reasons = Counter(row["reason"] for row in skipped)
        why = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        hint = "; pass --domain for another host" if reasons["other host"] else ""
        return {"error": f"No recorded read API calls to replay for {capture.domain}"
                         + (f" (skipped: {why}{hint})" if why else "")}
    answers, failed, answered = await _replay_all(replays, target)
    if not answered:
        return {"error": f"No response from {target}: {failed[0].error}"}
    changes = _changes(capture, answers, failed)
    return {
        "domain": capture.domain,
        "target": target,
        "routes": len({replay.route.name for replay in replays}),
        "replayed": len(replays),
        "answered": answered,
        "skipped": skipped,
        "breaking": sum(change["severity"] == drift.BREAKING for change in changes),
        "changes": changes,
    }
