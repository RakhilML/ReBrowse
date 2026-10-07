"""Execute stored endpoints over HTTP."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from rebrowse import config, net
from rebrowse.auth.vault import get_api_key, get_cookies
from rebrowse.models import EndpointDescriptor, ExecutionTrace, HttpMethod, SkillManifest, _now
from rebrowse.reverse.extractor import is_replay_header, same_site
from rebrowse.safety import Effect

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
BASE_DELAY = 1.0
MAX_DELAY = 10.0
TIMEOUT_S = 30.0

CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "challenge-platform", "captcha",
    "blocked by network security", "please respect our robot policy",
    "unusual traffic", "are you a robot", "access denied",
)

_last_request_at: dict[str, float] = {}


def _build_url(endpoint: EndpointDescriptor, params: dict | None = None) -> str:
    url = endpoint.url_template
    for key, val in {**(endpoint.path_params or {}), **(params or {})}.items():
        url = url.replace(f"{{{key}}}", str(val))
    return url


def _build_headers(endpoint: EndpointDescriptor, extra: dict | None = None) -> dict[str, str]:
    headers = {k: v for k, v in (endpoint.headers_template or {}).items() if is_replay_header(k)}
    headers["User-Agent"] = config.REBROWSE_UA
    if extra:
        headers.update(extra)
    return headers


def _cookie_header(cookies: list[dict], host: str) -> str:
    parts = []
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        domain = (c.get("domain") or "").lstrip(".").lower()
        if name and value and (not domain or host == domain or host.endswith("." + domain)):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _body_kwargs(endpoint: EndpointDescriptor, headers: dict[str, str]) -> dict:
    body = endpoint.body
    if body is None or endpoint.method in (HttpMethod.GET, HttpMethod.DELETE):
        return {}
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "").lower()
    if "application/x-www-form-urlencoded" in ctype and isinstance(body, dict):
        return {"data": body}
    if isinstance(body, (dict, list)):
        return {"json": body}
    return {"content": str(body)}


def _apply_credentials(skill: SkillManifest, url: str, headers: dict, query: dict) -> None:
    host = (urlparse(url).hostname or "").lower()
    first_party = same_site(url, skill.domain)

    cookies = get_cookies(skill.domain) if first_party else None
    if cookies:
        cookie = _cookie_header(cookies, host)
        if cookie:
            headers["Cookie"] = cookie

    key = get_api_key(host) or (get_api_key(skill.domain) if first_party else None)
    if key:
        add_api_key(key, headers, query)


def add_api_key(key: dict, headers: dict, query: dict) -> None:
    if key.get("auth_type") == "header":
        headers["X-API-Key"] = key["key"]
    elif key.get("auth_type") == "query":
        query["api_key"] = key["key"]
    else:
        headers["Authorization"] = f"Bearer {key['key']}"


async def pace(host: str) -> None:
    interval = config.HOST_MIN_INTERVAL_S
    if interval <= 0:
        return
    now = time.monotonic()
    wait = _last_request_at.get(host, 0.0) + interval - now
    _last_request_at[host] = now + max(wait, 0.0)
    if wait > 0:
        await asyncio.sleep(wait)


def _strip_off_site(origin: str):
    async def hook(request: httpx.Request) -> None:
        if not same_site(str(request.url), origin):
            for name in ("authorization", "cookie", "x-api-key"):
                request.headers.pop(name, None)
    return hook


def _sign_in_redirect(resp: httpx.Response, endpoint: EndpointDescriptor, origin: str) -> str | None:
    if not resp.history or "html" not in resp.headers.get("content-type", ""):
        return None
    if same_site(str(resp.url), origin) and endpoint.response_schema is None:
        return None
    return f"auth_required: redirected to an HTML page on {resp.url.host}; sign-in is likely needed"


def blocked_reason(resp: httpx.Response) -> str | None:
    if resp.status_code not in (401, 403, 429, 503):
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text/plain" not in ctype:
        return None
    head = resp.text[:4000].lower()
    marker = next((m for m in CHALLENGE_MARKERS if m in head), None)
    if not marker:
        return None
    return f"blocked: the site refused automated access (HTTP {resp.status_code}, '{marker}')"


def _parse_result(resp: httpx.Response) -> tuple[object, bool]:
    try:
        return resp.json(), False
    except ValueError:
        text = resp.text
        if len(text) > config.MAX_RESULT_CHARS:
            return text[:config.MAX_RESULT_CHARS], True
        return text, False


async def execute_endpoint(
    skill: SkillManifest,
    endpoint: EndpointDescriptor,
    params: dict | None = None,
    extra_headers: dict | None = None,
    confirmed: bool = False,
) -> ExecutionTrace:
    trace = ExecutionTrace(skill_id=skill.skill_id, endpoint_id=endpoint.endpoint_id)

    eff = endpoint.get_effect()
    if eff is not Effect.READ and not confirmed:
        trace.error = (
            f"confirmation_required: '{endpoint.method.value} {endpoint.url_template}' is a "
            f"{eff.value} operation and was NOT executed. Confirm explicitly to proceed."
        )
        trace.completed_at = _now()
        return trace

    url = _build_url(endpoint, params)
    headers = _build_headers(endpoint, extra_headers)
    query = dict(endpoint.query or {})
    _apply_credentials(skill, url, headers, query)
    body = _body_kwargs(endpoint, headers)
    host = (urlparse(url).hostname or "").lower()
    # A write that timed out may already have applied, so only reads are retried.
    retries = MAX_RETRIES if eff is Effect.READ else 0

    hooks = {"request": [_strip_off_site(host)]}
    async with net.client(TIMEOUT_S, follow_redirects=True, event_hooks=hooks) as client:
        for attempt in range(retries + 1):
            await pace(host)
            try:
                resp = await client.request(
                    endpoint.method.value, url, headers=headers, params=query, **body)
            except httpx.TimeoutException as e:
                trace.error = f"timeout: {e}"
                if attempt < retries:
                    await asyncio.sleep(min(BASE_DELAY * 2 ** attempt, MAX_DELAY))
                    continue
                break
            except httpx.HTTPError as e:
                trace.error = f"{type(e).__name__}: {e}"
                break

            trace.status_code = resp.status_code
            if resp.status_code in RETRYABLE_STATUSES and attempt < retries:
                await asyncio.sleep(min(BASE_DELAY * 2 ** attempt, MAX_DELAY))
                continue

            trace.result, trace.truncated = _parse_result(resp)
            refused = blocked_reason(resp) or _sign_in_redirect(resp, endpoint, host)
            trace.success = refused is None and 200 <= resp.status_code < 400
            if refused:
                trace.error = refused
            elif not trace.success:
                trace.error = f"HTTP {resp.status_code}"
            else:
                trace.error = None
            break

    trace.completed_at = _now()
    return trace
