"""Replay recorded reads against a server and report the changes that break their client."""

from __future__ import annotations

import asyncio
from collections import Counter
from http import HTTPStatus
from itertools import islice
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from rebrowse import config, drift, net
from rebrowse.auth.vault import get_api_key
from rebrowse.capture.har import content_type_of, credential_in_path, scrub_url, sendable_headers
from rebrowse.execution import executor
from rebrowse.mock import LOOPBACK, Recording, Route, build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import is_api_request
from rebrowse.reverse.graphql import request_effect
from rebrowse.safety import Effect, redact_body

MAX_REPLAYS_PER_ROUTE = 3
DEADLINE_FACTOR = 3
NO_RESPONSE, BLOCKED = "no_response", "blocked"
NON_API = "answered with a non-API response"
UNREPLAYABLE = "unreplayable"
_RECEIVED = ("content-encoding", "content-length", "transfer-encoding")


class Replay(NamedTuple):
    route: Route
    recordings: list[RawRequest]
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


def _skip_reason(route: Route, req: RawRequest, rec: Recording) -> str | None:
    if route.host:
        return "other host"
    effect = request_effect(req.method, req.url, rec.body)
    if effect is not Effect.READ:
        return effect.value
    if rec.status == HTTPStatus.NOT_MODIFIED:
        return "not modified"
    if "event-stream" in rec.content_type.lower():
        return "stream"
    return "credential in path" if credential_in_path(req.url) else None


def _candidates(route: Route, requests: list[RawRequest]) -> list[list[RawRequest]]:
    """The recordings of each distinct request to ROUTE that may be replayed."""
    groups: dict[tuple[str, str, str | None], list[RawRequest]] = {}
    for rec in route.recordings:
        req = requests[rec.index]
        if _skip_reason(route, req, rec) is None:
            key = (req.method, scrub_url(req.url) or req.url, req.request_body)
            groups.setdefault(key, []).append(req)
    return list(groups.values())


def _sent_from(recordings: list[RawRequest]) -> RawRequest:
    """The recording a request is sent as, whatever order its recordings were made in."""
    return min(recordings, key=lambda req: sorted(sendable_headers(req.request_headers).items()))


def _build(req: RawRequest, target: str) -> httpx.Request | None:
    try:
        return replay_request(req, target)
    except (httpx.InvalidURL, ValueError):
        return None


def _picks(route: Route, candidates: list[list[RawRequest]], target: str) -> list[Replay]:
    built = (Replay(route, group, request) for group in candidates
             if (request := _build(_sent_from(group), target)) is not None)
    return list(islice(built, MAX_REPLAYS_PER_ROUTE))


def _skipped(route: Route, requests: list[RawRequest], candidates: list[list[RawRequest]]) -> dict:
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


def _headers(recorded: dict[str, str]) -> dict[str, str]:
    return {**sendable_headers(recorded), "User-Agent": config.REBROWSE_UA}


def _query(recorded: str, key_query: dict[str, str]) -> str:
    if key_query:
        pairs = parse_qsl(recorded, keep_blank_values=True)
        recorded = urlencode([(name, value) for name, value in pairs if name not in key_query])
    return "&".join(part for part in (recorded, urlencode(key_query)) if part)


def _target_key(target: str) -> dict | None:
    return get_api_key(urlsplit(target).hostname or "")


def replay_request(req: RawRequest, target: str) -> httpx.Request:
    """Raises httpx.InvalidURL or ValueError for a recording that cannot be sent."""
    recorded = scrub_url(req.url)
    if recorded is None:
        raise ValueError("the recorded URL is not an http(s) URL without a credential")
    parts = urlsplit(recorded)
    headers = _headers(req.request_headers)
    key_query: dict[str, str] = {}
    if key := _target_key(target):
        executor.add_api_key(key, headers, key_query)
    query = _query(parts.query, key_query)
    url = f"{target}{parts.path or '/'}" + (f"?{query}" if query else "")
    body = None
    if req.method != "GET" and req.request_body is not None:
        body = redact_body(req.request_body, content_type_of(req.request_headers))
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
    live = _live(replay.recordings[0], resp)
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


def _regressed(recorded: set[int], live: int) -> bool:
    answered = {status for status in recorded if status < 500}
    if live in answered:
        return False
    if live >= 500:
        return bool(answered)
    return drift.status_breaks(recorded, {live})


def _regressions(answers: list[Answer]) -> list[dict]:
    statuses: dict[str, tuple[set[int], set[int]]] = {}
    for replay, live in answers:
        before = {req.response_status for req in replay.recordings}
        if _regressed(before, live.response_status):
            was, now = statuses.setdefault(replay.route.name, (set(), set()))
            was.update(before)
            now.add(live.response_status)
    return [{"severity": drift.BREAKING, "kind": "status", "route": route, "base": sorted(was),
             "head": sorted(now)} for route, (was, now) in statuses.items()]


def _side(capture: CaptureResult, requests: list[RawRequest]) -> CaptureResult:
    return CaptureResult(domain=capture.domain, final_url=capture.final_url, requests=requests)


def _changes(capture: CaptureResult, answers: list[Answer], failed: list[Failure]) -> list[dict]:
    base = _side(capture, [req for replay, _ in answers for req in replay.recordings])
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
        elif change["kind"] != "status":
            drifted.append(change)
        elif change["route"] not in regressed:
            drifted.append({**change, "severity": drift.INFO})
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
