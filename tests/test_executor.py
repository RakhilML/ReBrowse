from __future__ import annotations

import json
import time

from rebrowse import config
from rebrowse.auth.vault import store_api_key, store_cookies
from rebrowse.execution import executor
from rebrowse.execution.executor import _build_headers, execute_endpoint
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.safety import Effect


def _skill(domain="127.0.0.1"):
    return SkillManifest(name="t", domain=domain)


def test_replay_headers_are_honest_and_transport_free():
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template="https://example.com/api/x",
                            headers_template={
                                "User-Agent": "Mozilla/5.0 ... Chrome/131.0.0.0",
                                "sec-ch-ua": '"HeadlessChrome";v="140"',
                                "Content-Length": "999",
                                "Accept-Encoding": "gzip, br, zstd",
                                "Host": "example.com",
                                ":authority": "example.com",
                                "Accept": "application/json",
                                "x-app-token": "keep",
                            })
    headers = _build_headers(ep)
    assert headers["User-Agent"] == config.REBROWSE_UA
    lowered = {k.lower() for k in headers}
    assert not lowered & {"sec-ch-ua", "content-length", "accept-encoding", "host", ":authority"}
    assert headers["Accept"] == "application/json" and headers["x-app-token"] == "keep"
    assert _build_headers(ep, {"User-Agent": "custom/1"})["User-Agent"] == "custom/1"


async def test_form_body_is_replayed_as_form(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.POST, url_template=f"{fixture_site}/api/echo",
                            headers_template={"content-type": "application/x-www-form-urlencoded"},
                            body={"a": "1", "b": "two"}, effect=Effect.WRITE)
    trace = await execute_endpoint(_skill(), ep, confirmed=True)
    assert trace.success
    assert trace.result["received"] == "a=1&b=two"
    assert trace.result["content_type"].startswith("application/x-www-form-urlencoded")


async def test_json_body_is_replayed_as_json(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.POST, url_template=f"{fixture_site}/api/echo",
                            body={"text": "hi"}, effect=Effect.WRITE)
    trace = await execute_endpoint(_skill(), ep, confirmed=True)
    assert json.loads(trace.result["received"]) == {"text": "hi"}
    assert trace.result["content_type"] == "application/json"


async def test_challenge_page_is_a_failure_not_data(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/blocked")
    trace = await execute_endpoint(_skill(), ep)
    assert trace.success is False
    assert trace.error.startswith("blocked:")


async def test_large_text_result_is_truncated(fixture_site, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_CHARS", 1000)
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/big")
    trace = await execute_endpoint(_skill(), ep)
    assert trace.success and trace.truncated and len(trace.result) == 1000


async def test_http_error_is_reported(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/nope")
    trace = await execute_endpoint(_skill(), ep)
    assert not trace.success and trace.status_code == 404 and trace.error == "HTTP 404"


async def test_reads_retry_but_writes_do_not(fixture_site, hits, monkeypatch):
    monkeypatch.setattr(executor, "BASE_DELAY", 0.0)
    read = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/fail")
    write = EndpointDescriptor(method=HttpMethod.POST, url_template=f"{fixture_site}/api/fail",
                               body={"x": 1}, effect=Effect.WRITE)
    await execute_endpoint(_skill(), read)
    await execute_endpoint(_skill(), write, confirmed=True)
    assert hits[("GET", "/api/fail")] == executor.MAX_RETRIES + 1
    assert hits[("POST", "/api/fail")] == 1


async def test_credentials_go_only_to_their_host(fixture_site):
    store_api_key("127.0.0.1", "secret-key")
    store_cookies("127.0.0.1", [{"name": "sid", "value": "s1", "domain": "127.0.0.1"}])
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/echo")

    same = await execute_endpoint(_skill("127.0.0.1"), ep)
    assert same.result["authorization"] == "Bearer secret-key"
    assert same.result["cookie"] == "sid=s1"

    headers, query = {}, {}
    executor._apply_credentials(_skill("other-site.com"), "https://evil.example/x", headers, query)
    assert headers == {} and query == {}


async def test_off_site_redirect_gets_no_credentials_and_is_not_success(fixture_site):
    store_api_key("127.0.0.1", "secret-key", auth_type="header")
    store_cookies("127.0.0.1", [{"name": "sid", "value": "s1", "domain": "127.0.0.1"}])
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/sso")

    trace = await execute_endpoint(_skill(), ep)

    assert trace.result.endswith("<!---|-|--->")
    assert not trace.success and trace.error.startswith("auth_required:")


async def test_same_site_redirect_to_json_still_succeeds(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/moved")
    trace = await execute_endpoint(_skill(), ep)
    assert trace.success and len(trace.result["items"]) == 3


async def test_query_api_key(fixture_site):
    store_api_key("127.0.0.1", "k123", auth_type="query")
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/echo")
    trace = await execute_endpoint(_skill(), ep)
    assert "api_key=k123" in trace.result["query"]


async def test_per_host_pacing(fixture_site, monkeypatch):
    interval = 0.2
    monkeypatch.setattr(config, "HOST_MIN_INTERVAL_S", interval)
    executor._last_request_at.clear()
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/items")
    start = time.monotonic()
    for _ in range(3):
        await execute_endpoint(_skill(), ep)
    assert time.monotonic() - start >= 2 * interval - 0.05
