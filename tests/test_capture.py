from __future__ import annotations

from rebrowse.capture.store import load_capture, save_capture
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import extract_endpoints


def test_capture_save_load_roundtrip(tmp_path):
    cap = CaptureResult(
        domain="ex.com", final_url="https://ex.com/",
        requests=[RawRequest(
            url="https://ex.com/api/items", method="GET", response_status=200,
            response_headers={"content-type": "application/json"}, response_body='{"a": 1}',
        )],
        cookies=[{"name": "sid", "value": "secret"}],
        html="<html>big page</html>",
        js_bundles={"https://ex.com/app.js": "fetch('/api/hidden')"},
    )
    path = save_capture(cap, tmp_path / "cap.json")
    loaded = load_capture(path)

    assert [r.url for r in loaded.requests] == [r.url for r in cap.requests]
    assert loaded.js_bundles == cap.js_bundles
    assert loaded.html is None
    assert loaded.cookies == []

    before = [e.url_template for e in extract_endpoints(cap.requests, cap.domain)]
    after = [e.url_template for e in extract_endpoints(loaded.requests, loaded.domain)]
    assert before == after


async def test_capture_records_cookies_and_headers(fixture_site):
    from rebrowse.capture.browser import capture_session

    cap = await capture_session(fixture_site + "/", timeout_ms=15000)

    echo = [r for r in cap.requests if r.url.split("?")[0].endswith("/api/echo")]
    assert echo, "the /api/echo XHR was not captured"
    assert any("cookie" in {k.lower() for k in r.request_headers} for r in echo)
    assert any(c.get("name") == "sid" for c in cap.cookies)
    assert not any("/api/search" in r.url for r in cap.requests)
    assert any(u.endswith("/static/app.js?v=3") for u in cap.js_bundles)


async def test_steps_trigger_click_only_api(fixture_site):
    from rebrowse.capture.browser import capture_session

    cap = await capture_session(
        fixture_site + "/", timeout_ms=15000,
        steps="type #q=cats; click #go; wait 500",
    )
    assert any("/api/search" in r.url for r in cap.requests)
