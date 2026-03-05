"""Execute API skills — direct HTTP calls with retry logic."""

from __future__ import annotations
import json
import asyncio
import httpx
from rebrowse import config
from rebrowse.models import (
    EndpointDescriptor, SkillManifest, ExecutionTrace, HttpMethod, _id, _now,
)
from rebrowse.auth.vault import get_cookies, get_api_key
from rebrowse.auth.cookies import extract_browser_cookies


RETRYABLE_STATUSES = {500, 502, 503, 504, 429}
MAX_RETRIES = 2
BASE_DELAY = 1.0
MAX_DELAY = 10.0

_http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)


def _build_url(endpoint: EndpointDescriptor, params: dict | None = None) -> str:
    """Substitute path params into URL template."""
    url = endpoint.url_template
    merged = {**(endpoint.path_params or {}), **(params or {})}
    for key, val in merged.items():
        url = url.replace(f"{{{key}}}", str(val))
    return url


def _build_headers(endpoint: EndpointDescriptor, extra: dict | None = None) -> dict[str, str]:
    """Build request headers from template + extras."""
    headers = dict(endpoint.headers_template or {})
    if extra:
        headers.update(extra)
    # Ensure we have standard headers
    if "user-agent" not in {k.lower() for k in headers}:
        headers["User-Agent"] = config.CHROME_UA
    return headers


def _build_cookies_header(cookies: list[dict]) -> str:
    """Build Cookie header string from cookie list."""
    parts = []
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


async def execute_endpoint(
    skill: SkillManifest,
    endpoint: EndpointDescriptor,
    params: dict | None = None,
    extra_headers: dict | None = None,
) -> ExecutionTrace:
    """Execute a single endpoint with retry logic."""
    trace = ExecutionTrace(
        skill_id=skill.skill_id,
        endpoint_id=endpoint.endpoint_id,
    )

    url = _build_url(endpoint, params)
    headers = _build_headers(endpoint, extra_headers)

    # Try to get cookies for the domain
    cookies_list = get_cookies(skill.domain)
    if not cookies_list:
        # Try extracting from browser
        extraction = extract_browser_cookies(skill.domain)
        if extraction.cookies:
            cookies_list = [
                {"name": c.name, "value": c.value, "domain": c.domain}
                for c in extraction.cookies
            ]

    if cookies_list:
        headers["Cookie"] = _build_cookies_header(cookies_list)

    # Inject API key if stored for this domain
    api_key_info = get_api_key(skill.domain)
    if api_key_info:
        auth_type = api_key_info.get("auth_type", "bearer")
        key_value = api_key_info["key"]
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {key_value}"
        elif auth_type == "header":
            headers["X-API-Key"] = key_value
        # "query" type is handled below when building query params

    # Build query params
    query = dict(endpoint.query or {})
    if api_key_info and api_key_info.get("auth_type") == "query":
        query["api_key"] = api_key_info["key"]
    body = endpoint.body

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            if endpoint.method == HttpMethod.GET:
                resp = await _http.get(url, headers=headers, params=query)
            elif endpoint.method == HttpMethod.POST:
                resp = await _http.post(url, headers=headers, params=query, json=body)
            elif endpoint.method == HttpMethod.PUT:
                resp = await _http.put(url, headers=headers, params=query, json=body)
            elif endpoint.method == HttpMethod.PATCH:
                resp = await _http.patch(url, headers=headers, params=query, json=body)
            elif endpoint.method == HttpMethod.DELETE:
                resp = await _http.delete(url, headers=headers, params=query)
            else:
                resp = await _http.get(url, headers=headers, params=query)

            trace.status_code = resp.status_code

            if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES:
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                await asyncio.sleep(delay)
                continue

            # Parse response
            try:
                trace.result = resp.json()
            except Exception:
                trace.result = resp.text

            trace.success = 200 <= resp.status_code < 400
            trace.completed_at = _now()
            break

        except httpx.TimeoutException as e:
            last_error = f"Timeout: {e}"
            if attempt < MAX_RETRIES:
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                await asyncio.sleep(delay)
                continue
        except Exception as e:
            last_error = str(e)
            break

    if not trace.success and not trace.completed_at:
        trace.error = last_error or "Unknown error"
        trace.completed_at = _now()

    return trace


async def execute_skill(
    skill: SkillManifest,
    intent: str = "",
    params: dict | None = None,
) -> ExecutionTrace:
    """
    Execute the best matching endpoint in a skill.
    Uses simple keyword matching against endpoint URLs and descriptions.
    """
    if not skill.endpoints:
        return ExecutionTrace(
            skill_id=skill.skill_id,
            endpoint_id="none",
            error="Skill has no endpoints",
            completed_at=_now(),
        )

    # Rank endpoints by intent match
    best = skill.endpoints[0]
    best_score = 0.0

    if intent:
        intent_words = set(intent.lower().split())
        for ep in skill.endpoints:
            score = 0.0
            url_words = set(ep.url_template.lower().replace("/", " ").replace("-", " ").replace("_", " ").split())
            score += len(intent_words & url_words) * 2
            if ep.description:
                desc_words = set(ep.description.lower().split())
                score += len(intent_words & desc_words) * 3
            if ep.method == HttpMethod.GET:
                score += 1
            if score > best_score:
                best_score = score
                best = ep

    return await execute_endpoint(skill, best, params)
