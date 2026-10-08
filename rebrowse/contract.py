"""Replay recorded reads against a server and report the changes that break their client."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from itertools import islice
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit

import httpx

from rebrowse import config, drift, follow, net
from rebrowse.auth.vault import get_api_key
from rebrowse.capture.har import (
    HEADER_NAME,
    content_type_of,
    credential_in_path,
    scrub_url,
    sendable_headers,
)
from rebrowse.execution import executor
from rebrowse.mock import LOOPBACK, Recording, Route, build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import (
    canonical_template,
    is_api_request,
    normalize_url,
    parse_body,
)
from rebrowse.reverse.graphql import graphql_ops, request_effect
from rebrowse.safety import Effect, redact_body

MAX_REPLAYS_PER_ROUTE = 3
DEADLINE_FACTOR = 3
NO_RESPONSE, BLOCKED = "no_response", "blocked"
NON_API = "answered with a non-API response"
UNREPLAYABLE = "unreplayable"
ID_NOT_FOUND = "id not found on target"
_GONE = (404, 410)
_RECEIVED = ("content-encoding", "content-length", "transfer-encoding")
RESERVED_HEADERS = frozenset({
    "host", "user-agent", "content-length", "transfer-encoding", "connection", "keep-alive",
    "te", "trailer", "upgrade"})
_ENV_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Replay(NamedTuple):
    route: Route
    recordings: list[RawRequest]
    request: httpx.Request
    sent: RawRequest


class Failure(NamedTuple):
    route: str
    kind: str
    error: str


class Outcome(NamedTuple):
    """A report, how many routes answered 404 or 410 for an id from a recorded answer, and
    whether --follow-ids linked nothing although some read sends an id."""

    report: dict
    unfollowed: int
    unlinked: bool = False


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


def _env_header(spec: str, environ: Mapping[str, str]) -> tuple[str, str]:
    """Errors name only a reserved or NAME=ENVVAR-shaped header: a pasted value may be anywhere."""
    name, sep, variable = spec.partition("=")
    if not sep:
        raise ValueError("give NAME=ENVVAR, such as Cookie=SESSION_COOKIE")
    if not HEADER_NAME.fullmatch(name):
        raise ValueError("the text before '=' is not a header name")
    if name.lower() in RESERVED_HEADERS:
        raise ValueError(f"{name} cannot be set; rebrowse sends its own Host, User-Agent "
                         "and framing headers")
    if not _ENV_VAR.fullmatch(variable):
        raise ValueError("the text after '=' is not an environment variable name; give the "
                         "variable's name, not its value")
    value = environ.get(variable, "").strip()
    if not value:
        raise ValueError(f"header {name} names an environment variable that is unset or "
                         "empty; give the variable's name, not its value")
    if not (value.isascii() and value.isprintable()):
        raise ValueError(f"the value for header {name} has control or non-ASCII characters")
    return name, value


def env_headers(specs: Iterable[str], environ: Mapping[str, str]) -> dict[str, str]:
    """Header NAME with the value of ENVVAR for each NAME=ENVVAR; raises ValueError."""
    headers: dict[str, str] = {}
    for spec in specs:
        name, value = _env_header(spec, environ)
        if name.lower() in {given.lower() for given in headers}:
            raise ValueError(f"header {name} is given twice")
        headers[name] = value
    return headers


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


def _build(req: RawRequest, target: str, headers: Mapping[str, str]) -> httpx.Request | None:
    try:
        return replay_request(req, target, headers)
    except (httpx.InvalidURL, ValueError):
        return None


def _replay(route: Route, group: list[RawRequest], target: str,
            headers: Mapping[str, str]) -> Replay | None:
    recorded = _sent_from(group)
    request, sent = _build(recorded, target, headers), sent_request(recorded)
    return None if request is None or sent is None else Replay(route, group, request, sent)


def _picks(route: Route, candidates: list[list[RawRequest]], target: str,
           headers: Mapping[str, str]) -> list[Replay]:
    built = (replay for group in candidates
             if (replay := _replay(route, group, target, headers)) is not None)
    return list(islice(built, MAX_REPLAYS_PER_ROUTE))


def _skipped(route: Route, requests: list[RawRequest], candidates: list[list[RawRequest]]) -> dict:
    first = route.recordings[0]
    reason = UNREPLAYABLE if candidates else _skip_reason(route, requests[first.index], first)
    return {"route": route.name, "reason": reason}


def plan(capture: CaptureResult, target: str,
         headers: Mapping[str, str] | None = None) -> tuple[list[Replay], list[dict]]:
    replays: list[Replay] = []
    skipped: list[dict] = []
    for route in build_routes(capture, redirects=True):
        candidates = _candidates(route, capture.requests)
        if picks := _picks(route, candidates, target, headers or {}):
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


def _replaced(headers: dict[str, str], given: Mapping[str, str]) -> dict[str, str]:
    names = {name.lower() for name in given}
    return {**{k: v for k, v in headers.items() if k.lower() not in names}, **given}


def sent_request(req: RawRequest) -> RawRequest | None:
    """REQ with the URL and body a replay sends, or None for a URL that is never sent."""
    url = scrub_url(req.url)
    if url is None:
        return None
    body = None
    if req.method != "GET" and req.request_body is not None:
        body = redact_body(req.request_body, content_type_of(req.request_headers))
    return req.model_copy(update={"url": url, "request_body": body})


def replay_request(req: RawRequest, target: str,
                   headers: Mapping[str, str] | None = None) -> httpx.Request:
    """Raises httpx.InvalidURL or ValueError for a recording that cannot be sent."""
    clean = sent_request(req)
    if clean is None:
        raise ValueError("the recorded URL is not an http(s) URL without a credential")
    parts = urlsplit(clean.url)
    sent = _headers(req.request_headers)
    key_query: dict[str, str] = {}
    if key := _target_key(target):
        executor.add_api_key(key, sent, key_query)
    sent = _replaced(sent, headers or {})
    query = _query(parts.query, key_query)
    url = f"{target}{parts.path or '/'}" + (f"?{query}" if query else "")
    return httpx.Request(req.method, url, headers=sent, content=clean.request_body)


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


async def _exchange(client: httpx.AsyncClient, host: str, replay: Replay,
                    request: httpx.Request) -> tuple[bool, RawRequest | Failure]:
    """Whether ORIGIN answered REQUEST, and its live answer or the failure."""
    if host not in LOOPBACK:
        await executor.pace(host)
    try:
        resp = await _fetch(client, request)
    except httpx.HTTPError as e:
        return False, Failure(replay.route.name, NO_RESPONSE, f"{type(e).__name__}: {e}")
    except TimeoutError:
        deadline = executor.TIMEOUT_S * DEADLINE_FACTOR
        return False, Failure(replay.route.name, NO_RESPONSE,
                              f"TimeoutError: no complete answer within {deadline:g}s")
    return True, _judge(replay, resp)


def _route_key(req: RawRequest) -> tuple:
    parts = urlparse(req.url)
    ops = graphql_ops(parse_body(req.request_body), dict(parse_qsl(parts.query)))
    template = canonical_template(urlparse(normalize_url(req.url)[0]).path)
    return req.method, parts.netloc, template, tuple(op.dedup_token() for op in ops)


def _same_route(rewritten: RawRequest, recorded: RawRequest,
                values: Iterable[follow.Position]) -> bool:
    """Whether REWRITTEN, its followed path ids taken as placeholders, keys RECORDED's route."""
    holes = {position: "{}" for position in values if position.where == follow.PATH}
    return _route_key(follow.rewrite(rewritten, holes)) == _route_key(recorded)


def _followed_request(replay: Replay, links: list[follow.Link], bodies: Mapping[int, Any],
                      target: str, headers: Mapping[str, str]) -> httpx.Request | None:
    """REPLAY sending the ids ORIGIN answered, if each is safe and it still reads its route."""
    values: dict[follow.Position, str | int] = {}
    for link in links:
        if (value := follow.resolve(link, bodies.get(link.source))) is None:
            return None
        values[link.position] = value
    rewritten = follow.rewrite(replay.sent, values)
    if (scrubbed := sent_request(rewritten)) is None or scrubbed.url != rewritten.url:
        return None
    effect = request_effect(rewritten.method, rewritten.url, parse_body(rewritten.request_body))
    if effect is not Effect.READ or not _same_route(rewritten, replay.sent, values):
        return None
    return _build(rewritten, target, headers)


async def _replay_all(
    replays: list[Replay], order: list[int], links: list[follow.Link], target: str,
    headers: Mapping[str, str],
) -> tuple[dict[int, RawRequest | Failure], int]:
    """The outcome of each replay sent, in ORDER, and how many of them ORIGIN answered."""
    host = urlsplit(target).hostname or ""
    needs: dict[int, list[follow.Link]] = {}
    for link in links:
        needs.setdefault(link.dependent, []).append(link)
    sources = {link.source for link in links}
    bodies: dict[int, Any] = {}
    outcomes: dict[int, RawRequest | Failure] = {}
    answered = 0
    async with net.client(executor.TIMEOUT_S, follow_redirects=False) as client:
        for index in order:
            replay = replays[index]
            request = replay.request
            if index in needs:
                request = _followed_request(replay, needs[index], bodies, target, headers)
            if request is None:
                continue
            got, outcome = await _exchange(client, host, replay, request)
            answered += got
            outcomes[index] = outcome
            if (index in sources and isinstance(outcome, RawRequest)
                    and 200 <= outcome.response_status < 300):
                bodies[index] = follow.json_answer(outcome.response_body)
    return outcomes, answered


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


def _refusal(status: int) -> bool:
    return status in (401, 403) or (300 <= status < 400 and status != HTTPStatus.NOT_MODIFIED)


def _refusals(answers: list[Answer]) -> Counter[int]:
    """The refused statuses, when ORIGIN refused every request the recording answered."""
    refused: Counter[int] = Counter()
    for replay, live in answers:
        recorded = {req.response_status for req in replay.recordings}
        if not drift.answers(recorded):
            continue
        if not _refusal(live.response_status):
            return Counter()
        if live.response_status not in recorded:
            refused[live.response_status] += 1
    return refused


def _refused_error(target: str, refused: Counter[int], headers: Mapping[str, str],
                   variables: Mapping[str, str]) -> str:
    statuses = ", ".join(f"{status} x{count}" for status, count in sorted(refused.items()))
    error = f"{target} refused or redirected every read the recording answered ({statuses})"
    if not headers and _target_key(target):
        host = urlsplit(target).hostname
        return (f"{error} with the key stored for {host} by 'auth set'; it may have expired, "
                "or pass credentials with --header-env NAME=ENVVAR")
    if not headers:
        return (f"{error}; pass credentials with --header-env NAME=ENVVAR, such as "
                "--header-env Cookie=SESSION_COOKIE, or check ORIGIN's scheme and host")
    given = sorted(f"{name.lower()} from {variables[name]}" if name in variables
                   else name.lower() for name in headers)
    return (f"{error} with the credentials given ({', '.join(given)}); they may have expired "
            "or belong to another environment, or check ORIGIN's scheme and host")


def _followed_rows(replays: list[Replay], links: list[follow.Link]) -> list[dict]:
    rows = {(replays[link.dependent].route.name, link.position.where, link.position.name,
             replays[link.source].route.name, link.field) for link in links}
    return [dict(zip(("route", "in", "name", "from", "field"), row)) for row in sorted(rows)]


def _not_found(replays: list[Replay], sent: set[str]) -> list[dict]:
    unsent = {replay.route.name for replay in replays} - sent
    return [{"route": route, "reason": ID_NOT_FOUND} for route in sorted(unsent)]


def _reads(replays: list[Replay]) -> list[follow.Read]:
    return [follow.Read(replay.route.name, replay.sent, replay.recordings) for replay in replays]


def _unfollowed(replays: list[Replay], outcomes: Mapping[int, RawRequest | Failure]) -> int:
    """Routes whose read of an id another read's recorded answer held now answers 404 or 410."""
    gone = {index for index, live in outcomes.items()
            if isinstance(live, RawRequest) and live.response_status in _GONE
            and drift.answers({req.response_status for req in replays[index].recordings})}
    if not gone:
        return 0
    dependents = {link.dependent for link in follow.link(_reads(replays))[0]}
    return len({replays[index].route.name for index in gone & dependents})


async def check(capture: CaptureResult, target: str, headers: Mapping[str, str] | None = None,
                variables: Mapping[str, str] | None = None, follow_ids: bool = False) -> Outcome:
    """VARIABLES names the environment variable each of HEADERS was read from, for errors."""
    headers = headers or {}
    replays, skipped = plan(capture, target, headers)
    if not replays:
        reasons = Counter(row["reason"] for row in skipped)
        why = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        hint = "; pass --domain for another host" if reasons["other host"] else ""
        return Outcome({"error": f"No recorded read API calls to replay for {capture.domain}"
                                 + (f" (skipped: {why}{hint})" if why else "")}, 0)
    links, order = follow.link(_reads(replays)) if follow_ids else ([], list(range(len(replays))))
    outcomes, answered = await _replay_all(replays, order, links, target, headers)
    answers = [(replays[index], live) for index, live in outcomes.items()
               if isinstance(live, RawRequest)]
    failed = [failure for failure in outcomes.values() if isinstance(failure, Failure)]
    if not answered:
        return Outcome({"error": f"No response from {target}: {failed[0].error}"}, 0)
    if refused := _refusals(answers):
        return Outcome({"error": _refused_error(target, refused, headers, variables or {})}, 0)
    changes = _changes(capture, answers, failed)
    credentials = {"credentials": sorted(name.lower() for name in headers)} if headers else {}
    followed = {"followed": _followed_rows(replays, links)} if follow_ids else {}
    sent = {replays[index].route.name for index in outcomes}
    report = {
        "domain": capture.domain,
        "target": target,
        **credentials,
        "routes": len(sent),
        "replayed": len(outcomes),
        "answered": answered,
        **followed,
        "skipped": skipped + _not_found(replays, sent),
        "breaking": sum(change["severity"] == drift.BREAKING for change in changes),
        "changes": changes,
    }
    if not follow_ids:
        return Outcome(report, _unfollowed(replays, outcomes))
    unlinked = not links and any(follow.positions(replay.sent) for replay in replays)
    return Outcome(report, 0, unlinked)


async def run_contract(capture: CaptureResult, target: str,
                       headers: Mapping[str, str] | None = None,
                       variables: Mapping[str, str] | None = None,
                       follow_ids: bool = False) -> dict:
    return (await check(capture, target, headers, variables, follow_ids)).report
