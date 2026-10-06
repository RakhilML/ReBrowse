from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit

import pytest
from click.testing import CliRunner
from playwright.async_api import async_playwright
from test_openapi import assert_valid

from rebrowse import config
from rebrowse.auth.vault import get_cookies
from rebrowse.capture.har import HarError, load_har
from rebrowse.cli import main
from rebrowse.execution.executor import execute_endpoint
from rebrowse.llm.client import LLMError
from rebrowse.models import EndpointDescriptor, SkillManifest
from rebrowse.openapi import skill_to_openapi
from rebrowse.orchestrator import pipeline
from rebrowse.safety import REDACTED, is_secret_name
from rebrowse.store.skills import list_all_skills, resolve_skill, save_skill

FORM = "application/x-www-form-urlencoded"
CREDENTIAL_HEADERS = [
    ("Cookie", "sid=COOKIE_SECRET"), ("Authorization", "Bearer BEARER_SECRET"),
    ("X-API-Key", "KEY_SECRET"), ("X-CSRF-Token", "CSRF_SECRET"), ("X-Auth", "AUTH_SECRET"),
    ("X-Session-Id", "SESSION_SECRET"),
]
APP_JS = 'function recs(){ return fetch("/api/recommendations"); }'


def _pairs(items) -> list[dict]:
    return [{"name": name, "value": value} for name, value in items]


def _json_post(payload) -> dict:
    return {"mimeType": "application/json", "text": json.dumps(payload)}


def _entry(method: str, url: str, *, status: int = 200, mime: str = "application/json",
           body: str | None = None, headers=(), response_headers=(), post: dict | None = None,
           encoding: str | None = None) -> dict:
    content = {"size": len(body or ""), "mimeType": mime}
    if body is not None:
        content["text"] = body
    if encoding:
        content["encoding"] = encoding
    request = {"method": method, "url": url, "httpVersion": "HTTP/1.1",
               "headers": _pairs(headers), "queryString": [], "cookies": []}
    if post is not None:
        request["postData"] = post
    return {
        "startedDateTime": "2026-10-06T10:00:00.000Z", "time": 5, "request": request,
        "response": {"status": status, "statusText": "", "httpVersion": "HTTP/1.1",
                     "headers": _pairs(response_headers), "cookies": [], "content": content},
    }


def _write(tmp_path: Path, entries: list, name: str = "session.har") -> Path:
    path = tmp_path / name
    log = {"version": "1.2", "creator": {"name": "test", "version": "1"}, "entries": entries}
    path.write_text(json.dumps({"log": log}), encoding="utf-8")
    return path


def _with_cookies(entry: dict) -> dict:
    entry["request"]["cookies"] = [{"name": "sid", "value": "COOKIE_SECRET"}]
    entry["response"]["cookies"] = [{"name": "sid", "value": "SETCOOKIE_SECRET"}]
    return entry


def _by_path(capture) -> dict:
    return {urlsplit(r.url).path: r for r in capture.requests}


@pytest.fixture
def described(monkeypatch) -> list:
    calls: list = []

    async def describe(endpoints):
        calls.append(endpoints)
        return [{**e, "description": f"stub: {e['method']} {e['url_template']}"} for e in endpoints]

    monkeypatch.setattr(pipeline, "describe_endpoints", describe)
    return calls


def test_converts_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 64)
    ok = json.dumps({"ok": True})
    har = _write(tmp_path, [
        "junk",
        _entry("GET", "https://ann:pw@app.local/#top", mime="text/html", body="<html></html>"),
        _entry("get", "https://app.local/api/items", body='{"items": []}', headers=[
            ("Accept", "application/json"), ("User-Agent", "Mozilla/5.0"),
            ("X-Trace", "a"), ("x-trace", "b")]),
        _entry("GET", "https://app.local/api/b64", mime="", encoding="base64",
               body=base64.b64encode(ok.encode()).decode(),
               response_headers=[("Content-Type", "application/json; charset=utf-8")]),
        _entry("GET", "https://app.local/api/big", body=json.dumps({"x": "y" * 100})),
        _entry("GET", "https://app.local/api/cancelled", status=0),
        {"request": {"method": "GET", "url": "https://app.local/api/orphan"}},
        {"request": {"method": "GET", "url": 7}, "response": {"status": 200}},
        _entry("GET", "wss://app.local/socket", status=101),
        _entry("GET", "https://app.local/bundle?v=3", mime="application/javascript",
               body='fetch("/api/a")'),
        _entry("GET", "https://app.local/static/app.js", mime="text/plain", body='fetch("/api/b")'),
    ])

    capture = load_har(har)

    assert [r.url for r in capture.requests] == [
        "https://app.local/", "https://app.local/api/items",
        "https://app.local/api/b64", "https://app.local/api/big"]
    reqs = _by_path(capture)
    items = reqs["/api/items"]
    assert items.method == "GET"
    assert items.request_headers == {"accept": "application/json", "x-trace": "a, b"}
    assert items.response_headers == {"content-type": "application/json"}
    assert items.response_body == '{"items": []}'
    assert reqs["/api/b64"].response_headers == {"content-type": "application/json; charset=utf-8"}
    assert reqs["/api/b64"].response_body == ok
    assert reqs["/api/big"].response_body is None
    assert capture.js_bundles == {
        "https://app.local/bundle?v=3": 'fetch("/api/a")',
        "https://app.local/static/app.js": 'fetch("/api/b")',
    }
    assert (capture.domain, capture.final_url) == ("app.local", "https://app.local/")


def test_strips_credentials_at_conversion(tmp_path):
    search = _with_cookies(_entry(
        "GET", "https://app.local/api/search?access_token=TOKEN_SECRET&q=cats",
        headers=[*CREDENTIAL_HEADERS, ("Accept", "application/json"),
                 ("Referer", "https://app.local/reset?token=REFERER_SECRET&step=2")],
        response_headers=[("Set-Cookie", "sid=SETCOOKIE_SECRET"),
                          ("Content-Type", "application/json")]))
    plain = "https://app.local/api/list?page=2&sort=a%20b&tags=x+y"
    har = _write(tmp_path, [
        search,
        _entry("POST", "https://app.local/api/login", post=_json_post(
            {"user": "ann", "password": "hunter2", "nested": {"apiKey": "KEY_SECRET"}})),
        _entry("POST", "https://app.local/api/form",
               post={"mimeType": FORM, "text": "username=a&password=hunter2"}),
        _entry("POST", "https://app.local/api/params",
               post={"mimeType": FORM, "params": _pairs([("user", "a"), ("pwd", "hunter2")])}),
        _entry("POST", "https://app.local/api/upload", post={
            "mimeType": "multipart/form-data; boundary=x",
            "text": '--x\r\nContent-Disposition: form-data; name="password"\r\n\r\n'
                    'hunter2\r\n--x--'}),
        _entry("GET", plain),
    ])

    capture = load_har(har)

    reqs = _by_path(capture)
    found = reqs["/api/search"]
    assert set(found.request_headers) == {"accept", "referer"}
    referer = urlsplit(found.request_headers["referer"]).query
    assert parse_qs(referer) == {"token": [REDACTED], "step": ["2"]}
    assert found.response_headers == {"content-type": "application/json"}
    assert capture.cookies == []
    assert parse_qs(urlsplit(found.url).query) == {"access_token": [REDACTED], "q": ["cats"]}
    assert json.loads(reqs["/api/login"].request_body) == {
        "user": "ann", "password": REDACTED, "nested": {"apiKey": REDACTED}}
    assert parse_qsl(reqs["/api/form"].request_body) == [("username", "a"), ("password", REDACTED)]
    assert parse_qsl(reqs["/api/params"].request_body) == [("user", "a"), ("pwd", REDACTED)]
    assert reqs["/api/upload"].request_body is None
    assert reqs["/api/list"].url == plain
    dumped = capture.model_dump_json()
    assert "_SECRET" not in dumped and "hunter2" not in dumped


def test_domain_inference(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", "https://cdn.other.net/api/config"),
        _entry("GET", "https://app.local:8443/home", mime="text/html; charset=utf-8", body="<p>"),
        _entry("GET", "https://admin.local/api/stats"),
        _entry("GET", "https://admin.local/dashboard", mime="text/html", body="<p>"),
    ])

    found = load_har(har)
    assert (found.domain, found.final_url) == ("app.local:8443", "https://app.local:8443/home")
    assert len(found.requests) == 4

    chosen = load_har(har, domain="admin.local")
    assert (chosen.domain, chosen.final_url) == ("admin.local", "https://admin.local/dashboard")
    assert load_har(har, domain="cdn.other.net").final_url == "https://cdn.other.net/api/config"
    with pytest.raises(HarError, match="nothere.test"):
        load_har(har, domain="nothere.test")

    xhr_only = _write(tmp_path, [_entry("GET", "http://api.local:8080/v1/x")], "xhr.har")
    assert load_har(xhr_only).domain == "api.local:8080"


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b'{"log": {}}', b'{"log": {"entries": {}}}', b"\xff\xfe{}",
])
def test_rejects_malformed_har(tmp_path, raw):
    path = tmp_path / "bad.har"
    path.write_bytes(raw)
    with pytest.raises(HarError):
        load_har(path)


def test_loads_a_har_with_a_utf8_bom(tmp_path):
    path = _write(tmp_path, [_entry("GET", "https://app.local/api/items")])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert load_har(path).domain == "app.local"


def _session(site: str) -> list[dict]:
    return [
        _entry("GET", "https://cdn.thirdparty.test/api/widgets", body='{"widgets": []}'),
        _entry("GET", f"{site}/", mime="text/html", body="<html><body>app</body></html>"),
        _entry("GET", f"{site}/static/app.js?v=3", mime="application/javascript", body=APP_JS),
        _entry("GET", f"{site}/api/items", body=json.dumps({"items": [{"id": 1}]})),
        _entry("POST", f"{site}/api/notes", post=_json_post({"text": "hi"}), body='{"ok": true}'),
        _entry("GET", f"{site}/api/users/101",
               body=json.dumps({"id": 101, "name": "ann", "email": "ann@app.test"})),
        _entry("GET", f"{site}/api/users/102", body=json.dumps({"id": 102, "name": "bob"})),
        _entry("POST", f"{site}/graphql", body='{"data": {"feed": []}}', post=_json_post(
            {"operationName": "Feed", "query": "query Feed { feed { id } }"})),
        _entry("POST", f"{site}/graphql", body='{"data": {"like": {"ok": true}}}', post=_json_post(
            {"operationName": "Like", "query": "mutation Like { like(id: 1) { ok } }"})),
        _entry("POST", f"{site}/api/collect", post=_json_post({"event": "view"}), body="{}"),
    ]


async def test_import_har_end_to_end(tmp_path, fixture_site, hits, described):
    har = _write(tmp_path, _session(fixture_site))

    result = await pipeline.import_har(har)

    domain = urlsplit(fixture_site).netloc
    assert result["domain"] == domain and result["replaced"] is False
    assert result["source"] == str(har.resolve())
    eps = {(e["method"], urlsplit(e["url"]).path): e for e in result["endpoints"]}
    assert eps[("GET", "/api/items")]["effect"] == "read"
    assert eps[("POST", "/api/notes")]["effect"] == "write"
    assert ("GET", "/api/recommendations") in eps
    assert not any(path.startswith("/api/collect") for _, path in eps)
    assert all(fixture_site in e["url"] for e in result["endpoints"])
    graphql = sorted((e["description"], e["effect"]) for e in result["endpoints"]
                     if e["url"].endswith("/graphql"))
    assert graphql == [("GraphQL mutation: Like", "write"), ("GraphQL query: Feed", "read")]
    assert all(e["description"].startswith("stub:") for e in result["endpoints"]
               if not e["url"].endswith("/graphql"))

    skill = resolve_skill(domain)
    users = next(ep for ep in skill.endpoints if ep.url_template.endswith("/api/users/{users_id}"))
    assert set(users.response_schema.properties) == {"id", "name", "email"}
    assert users.response_schema.required == ["id", "name"]
    assert skill.intent_signature == f"{fixture_site}/"
    assert sum(hits.values()) == 0

    again = await pipeline.import_har(har)
    assert again["replaced"] is True and again["skill_id"] == result["skill_id"]
    assert len(list_all_skills()) == 1


async def test_import_har_keeps_secrets_out(tmp_path, isolated, described):
    login = _with_cookies(_entry(
        "POST", "https://app.local/api/login", body='{"ok": true}',
        headers=[*CREDENTIAL_HEADERS, ("Content-Type", "application/json")],
        post=_json_post({"user": "ann", "password": "hunter2"}),
        response_headers=[("Set-Cookie", "sid=SETCOOKIE_SECRET; HttpOnly"),
                          ("Content-Type", "application/json")]))
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>"),
        login,
        _entry("GET", "https://app.local/api/me?access_token=TOKEN_SECRET&fields=name",
               headers=CREDENTIAL_HEADERS, body='{"name": "ann"}'),
    ])

    result = await pipeline.import_har(har)

    assert result["endpoints_count"] == 2
    stored = [p for p in isolated.rglob("*") if p.is_file()]
    assert any(p.name == "skills.db" for p in stored)
    for path in stored:
        data = path.read_bytes()
        assert b"_SECRET" not in data and b"hunter2" not in data, path
    assert get_cookies("app.local") is None
    assert not any((isolated / "captures").iterdir())
    for text in (json.dumps(described), json.dumps(skill_to_openapi(resolve_skill("app.local")))):
        assert "_SECRET" not in text and "hunter2" not in text


async def test_import_har_without_api_traffic(tmp_path, described):
    only_bundle = _write(tmp_path, [
        _entry("GET", "https://app.local/app.js", mime="text/javascript", body="var x = 1;")])
    only_page = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>")], "page.har")

    assert (await pipeline.import_har(only_bundle))["error"].startswith("No requests")
    assert (await pipeline.import_har(only_page))["error"].startswith("No API endpoints")
    assert list_all_skills() == []


def test_cli_import_har(tmp_path, described):
    runner = CliRunner()
    har = _write(tmp_path, [
        _entry("GET", "http://app.local/", mime="text/html", body="<html></html>"),
        _entry("GET", "http://app.local/api/items?page=1", body='{"items": [{"id": 1}]}'),
        _entry("POST", "http://app.local/api/items", post=_json_post({"name": "x"}),
               body='{"id": 2}'),
    ])
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a har"}', encoding="utf-8")

    result = runner.invoke(main, ["import-har", str(har)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["skill_id"] and payload["endpoints_count"] == 2
    assert payload["source"] == str(har.resolve())

    failed = runner.invoke(main, ["import-har", str(bad)])
    assert failed.exit_code == 1 and "error" in json.loads(failed.stdout)
    assert runner.invoke(main, ["import-har", str(tmp_path / "missing.har")]).exit_code == 2
    assert runner.invoke(main, ["import-har", str(har), "-d", "nothere.test"]).exit_code == 1

    exported = runner.invoke(main, ["openapi", "app.local"])
    assert exported.exit_code == 0, exported.output
    assert_valid(json.loads(exported.stdout))


def test_group_help_lists_import_har():
    assert "import-har <har>  build a skill from a HAR export" in CliRunner().invoke(
        main, ["--help"]).output


def test_skips_pseudo_headers_and_non_http_entries(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>", headers=[
            (":authority", "app.local"), (":method", "GET"), (":path", "/"), (":scheme", "https"),
            ("accept", "text/html")]),
        _entry("GET", "data:image/png;base64,AAAA", mime="image/png"),
        _entry("GET", "chrome-extension://abc/content.js", mime="application/javascript", body="x"),
        _entry("GET", "blob:https://app.local/1234", body="{}"),
    ])

    capture = load_har(har)

    assert [r.url for r in capture.requests] == ["https://app.local/"]
    assert capture.requests[0].request_headers == {"accept": "text/html"}
    assert capture.js_bundles == {}


@pytest.mark.parametrize("name", ["X-UserAuth", "X-ClientSid", "X-DeviceOtp"])
def test_drops_camel_case_secret_headers(tmp_path, name):
    assert is_secret_name(name)
    har = _write(tmp_path, [_entry("GET", "https://app.local/api/me",
                                   headers=[(name, "HEADER_SECRET"), ("Accept", "*/*")])])

    assert load_har(har).requests[0].request_headers == {"accept": "*/*"}


def test_redacts_graphql_get_variables_and_referer(tmp_path):
    variables = json.dumps({"token": "GQL_SECRET", "id": 1})
    url = "https://app.local/graphql?" + urlencode({"operationName": "Me", "variables": variables})
    har = _write(tmp_path, [_entry("GET", url, headers=[
        ("Referer", "https://ann:REF_SECRET@app.local/inbox?sig=SIG_SECRET&tab=2#msg")])])

    request = load_har(har).requests[0]

    query = dict(parse_qsl(urlsplit(request.url).query))
    assert query["operationName"] == "Me"
    assert json.loads(query["variables"]) == {"token": REDACTED, "id": 1}
    referer = urlsplit(request.request_headers["referer"])
    assert (referer.netloc, referer.path, referer.fragment) == ("app.local", "/inbox", "")
    assert parse_qsl(referer.query) == [("sig", REDACTED), ("tab", "2")]


def test_request_body_mime_falls_back_and_unredactable_bodies_drop(tmp_path):
    har = _write(tmp_path, [
        _entry("POST", "https://app.local/api/header-form", headers=[("Content-Type", FORM)],
               post={"text": "a=1&token=FORM_SECRET"}),
        _entry("POST", "https://app.local/api/beacon", post={
            "mimeType": "text/plain;charset=UTF-8", "text": '{"password": "BEACON_SECRET"}'}),
        _entry("POST", "https://app.local/api/xml", post={
            "mimeType": "application/xml", "text": "<password>XML_SECRET</password>"}),
        _entry("POST", "https://app.local/api/both", post={
            "mimeType": FORM, "text": "q=cats&pwd=TEXT_SECRET", "params": _pairs([("q", "dogs")])}),
    ])

    reqs = _by_path(load_har(har))

    assert parse_qsl(reqs["/api/header-form"].request_body) == [("a", "1"), ("token", REDACTED)]
    assert json.loads(reqs["/api/beacon"].request_body) == {"password": REDACTED}
    assert reqs["/api/xml"].request_body is None
    assert parse_qsl(reqs["/api/both"].request_body) == [("q", "cats"), ("pwd", REDACTED)]


def test_bundles_are_decoded_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_JS_BUNDLES", 2)
    monkeypatch.setattr(config, "MAX_BUNDLE_CHARS", 40)
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>"),
        _entry("GET", "https://app.local/a.js", mime="text/javascript", encoding="base64",
               body=base64.b64encode(b'fetch("/api/a")').decode()),
        _entry("GET", "https://app.local/huge.mjs", mime="application/javascript", body="x" * 40),
        _entry("GET", "https://app.local/b.js", mime="", body='fetch("/api/b")'),
        _entry("GET", "https://app.local/c.js", body='fetch("/api/c")'),
    ])

    capture = load_har(har)

    assert capture.js_bundles == {
        "https://app.local/a.js": 'fetch("/api/a")', "https://app.local/b.js": 'fetch("/api/b")'}
    assert [r.url for r in capture.requests] == ["https://app.local/"]


def test_bundles_come_only_from_the_chosen_site(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_JS_BUNDLES", 2)
    jsonp = "https://admin.internal.test/api/data?callback=cb"
    har = _write(tmp_path, [
        _entry("GET", "https://sso.corp.test/login", mime="text/html", body="<p>"),
        _entry("GET", "https://sso.corp.test/static/login.js", body='fetch("/api/authn/verify")'),
        *[_entry("GET", f"https://cdn{i}.vendor.test/lib.js", body="x") for i in range(3)],
        _entry("GET", "https://admin.internal.test/", mime="text/html", body="<p>"),
        _entry("GET", "https://static.internal.test/admin.js", body='fetch("/api/orders")'),
        _entry("GET", "https://static.internal.test/admin.js", body=""),
        _entry("GET", jsonp, mime="application/javascript", body="cb({})"),
    ])

    capture = load_har(har, domain="admin.internal.test")

    assert capture.js_bundles == {"https://static.internal.test/admin.js": 'fetch("/api/orders")'}
    assert jsonp in [r.url for r in capture.requests]


def test_drops_session_path_params_sso_callbacks_and_credential_values(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/app/home.do;jsessionid=JSESS_SECRET;v=2",
               mime="text/html", body="<p>"),
        _entry("GET", "https://app.local/oauth/callback?code=CODE_SECRET&state=s1"),
        _entry("GET", "https://app.local/cas/login?ticket=ST-TICKET_SECRET&service=x"),
        _entry("GET", "https://app.local/api/promo?code=SPRING&ticket=42"),
        _entry("GET", "https://app.local/api/me", headers=[
            ("Authentication", "Bearer AUTHN_SECRET"), ("X-Hmac", "HMAC HMAC_SECRET"),
            ("X-Trace", "eyJhbGciOi.eyJzdWIi.JWT_SECRET"), ("Accept", "application/json")]),
    ])

    capture = load_har(har)

    reqs = _by_path(capture)
    assert capture.final_url == "https://app.local/app/home.do;v=2"
    assert parse_qs(urlsplit(reqs["/oauth/callback"].url).query) == {
        "code": [REDACTED], "state": ["s1"]}
    assert parse_qs(urlsplit(reqs["/cas/login"].url).query) == {
        "ticket": [REDACTED], "service": ["x"]}
    assert reqs["/api/promo"].url == "https://app.local/api/promo?code=SPRING&ticket=42"
    assert reqs["/api/me"].request_headers == {"accept": "application/json"}
    assert "_SECRET" not in capture.model_dump_json()


def test_domain_option_accepts_urls_and_bare_hosts(tmp_path):
    har = _write(tmp_path, [_entry("GET", "https://app.local:8080/api/items"),
                            _entry("GET", "https://other.local/api/x")])

    for given in ("https://app.local:8080/", "app.local", "APP.local:8080"):
        assert load_har(har, domain=given).domain == "app.local:8080"
    assert load_har(har, domain="other.local:443").domain == "other.local"
    with pytest.raises(HarError, match=r"hosts seen: app\.local:8080, other\.local"):
        load_har(har, domain="nothere.test")


def test_deeply_nested_input_is_rejected_or_dropped(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    path = tmp_path / "deep.har"
    path.write_text('{"log": {"entries": ' + deep + "}}", encoding="utf-8")
    with pytest.raises(HarError):
        load_har(path)

    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<p>"),
        _entry("POST", "https://app.local/api/deep",
               post={"mimeType": "application/json", "text": deep}),
    ])
    assert [r.url for r in load_har(har).requests] == ["https://app.local/"]


def test_null_form_params_become_empty_values(tmp_path):
    post = {"mimeType": FORM, "params": [{"name": "a", "value": None}, {"name": "b", "value": "1"}]}
    har = _write(tmp_path, [_entry("POST", "https://app.local/api/f", post=post)])
    assert _by_path(load_har(har))["/api/f"].request_body == "a=&b=1"


def test_domain_option_ignores_case(tmp_path):
    har = _write(tmp_path, [_entry("GET", "https://App.Local:8443/api/items")])

    assert load_har(har, domain="APP.local:8443").domain == "app.local:8443"


async def test_import_har_replaces_a_built_skill(tmp_path, described):
    built = SkillManifest(
        name="app.local API", domain="app.local", intent_signature="https://app.local/",
        endpoints=[EndpointDescriptor(url_template="https://app.local/api/old")])
    save_skill(built)
    har = _write(tmp_path, [_entry("GET", "https://app.local/api/items", body='{"items": []}')])

    result = await pipeline.import_har(har)

    assert result["replaced"] is True and result["skill_id"] == built.skill_id
    skill = resolve_skill("app.local")
    assert skill.created_at == built.created_at
    assert [ep.url_template for ep in skill.endpoints] == ["https://app.local/api/items"]
    assert len(list_all_skills()) == 1


async def test_import_har_redacts_the_document_url(tmp_path, isolated, described):
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/home?session=DOC_SECRET#top", mime="text/html",
               body="<html></html>"),
        _entry("GET", "https://app.local/api/items", body='{"items": []}'),
    ])

    result = await pipeline.import_har(har)

    skill = resolve_skill("app.local")
    assert skill.intent_signature == f"https://app.local/home?session={quote(REDACTED)}"
    assert "DOC_SECRET" not in json.dumps(result)
    assert not any(b"DOC_SECRET" in p.read_bytes() for p in isolated.rglob("*") if p.is_file())


def test_redacts_bracketed_and_percent_encoded_secret_names(tmp_path):
    url = ("https://app.local/api/s?user%5Bpassword%5D=P1_SECRET&access%5Ftoken=P2_SECRET"
           "&tag=a&tag=b")
    har = _write(tmp_path, [_entry("GET", url)])

    request = load_har(har).requests[0]

    assert parse_qsl(urlsplit(request.url).query) == [
        ("user[password]", REDACTED), ("access_token", REDACTED), ("tag", "a"), ("tag", "b")]


async def test_json_sent_with_a_form_content_type_is_redacted(tmp_path, isolated, described):
    form = f"{FORM}; charset=UTF-8"
    har = _write(tmp_path, [_entry(
        "POST", "https://app.local/api/login", headers=[("Content-Type", form)], body='{"ok": 1}',
        post={"mimeType": form, "text": json.dumps({"user": "ann", "password": "hunter2"})})])

    assert "hunter2" not in (load_har(har).requests[0].request_body or "")

    await pipeline.import_har(har)
    assert not any(b"hunter2" in p.read_bytes() for p in isolated.rglob("*") if p.is_file())
    assert "hunter2" not in json.dumps(skill_to_openapi(resolve_skill("app.local")))


def test_json_keys_in_query_and_form_pairs_are_redacted(tmp_path):
    payload = json.dumps({"apiKey": "QUERY_SECRET", "q": "cats"})
    har = _write(tmp_path, [
        _entry("GET", f"https://app.local/api/s?{quote(payload)}"),
        _entry("POST", "https://app.local/api/form", post={
            "mimeType": FORM, "text": quote(json.dumps({"password": "FORM_SECRET"}))}),
    ])

    reqs = _by_path(load_har(har))

    [(key, _)] = parse_qsl(urlsplit(reqs["/api/s"].url).query, keep_blank_values=True)
    assert json.loads(key) == {"apiKey": REDACTED, "q": "cats"}
    [(key, _)] = parse_qsl(reqs["/api/form"].request_body, keep_blank_values=True)
    assert json.loads(key) == {"password": REDACTED}


async def test_import_har_redacts_saml_and_pw_logins(tmp_path, isolated, described):
    assertion = base64.b64encode(
        b'<samlp:Response ID="r1"><Assertion>ann</Assertion></samlp:Response>').decode()
    acs = urlencode({"SAMLResponse": assertion, "RelayState": "/admin"})
    har = _write(tmp_path, [
        _entry("GET", "https://admin.local/", mime="text/html", body="<html></html>"),
        _entry("POST", "https://admin.local/login", body='{"ok": true}',
               post={"mimeType": FORM, "text": "user=ann&pw=PW_SECRET"}),
        _entry("POST", "https://admin.local/saml/acs", body='{"ok": true}',
               post={"mimeType": FORM, "text": acs}),
        _entry("GET", "https://admin.local/api/items", body='{"items": []}'),
    ])

    reqs = _by_path(load_har(har))
    assert parse_qsl(reqs["/login"].request_body) == [("user", "ann"), ("pw", REDACTED)]
    assert parse_qsl(reqs["/saml/acs"].request_body) == [
        ("SAMLResponse", REDACTED), ("RelayState", "/admin")]

    result = await pipeline.import_har(har)

    learned = {urlsplit(e["url"]).path for e in result["endpoints"]}
    assert {"/login", "/saml/acs"} <= learned
    secrets = (b"PW_SECRET", assertion[:20].encode())
    for path in (p for p in isolated.rglob("*") if p.is_file()):
        data = path.read_bytes()
        assert not any(secret in data for secret in secrets), path
    exported = json.dumps(skill_to_openapi(resolve_skill("admin.local")))
    assert not any(secret.decode() in exported for secret in secrets)


async def test_imported_endpoints_verify_and_replay_honestly(tmp_path, fixture_site, hits,
                                                            described):
    har = _write(tmp_path, [
        _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{fixture_site}/api/echo?q=1", headers=CREDENTIAL_HEADERS,
               body='{"method": "GET"}'),
        _entry("POST", f"{fixture_site}/api/notes", headers=CREDENTIAL_HEADERS,
               post=_json_post({"text": "hi"}), body='{"ok": true}'),
    ])
    domain = urlsplit(fixture_site).netloc
    await pipeline.import_har(har)

    report = await pipeline.verify(domain)

    assert (report["verified"], report["skipped"]) == (1, 1)
    assert dict(hits) == {("GET", "/api/echo"): 1}
    skill = resolve_skill(domain)
    echo = next(ep for ep in skill.endpoints if ep.url_template.endswith("/api/echo"))
    trace = await execute_endpoint(skill, echo)
    assert trace.result["user_agent"] == config.REBROWSE_UA
    assert trace.result["cookie"] is None and trace.result["authorization"] is None


async def _record_har(site: str, path: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(record_har_path=str(path))
        page = await context.new_page()
        await page.goto(f"{site}/")
        await page.wait_for_load_state("networkidle")
        await page.fill("#q", "cats")
        async with page.expect_response(lambda r: "/api/search" in r.url):
            await page.click("#go")
        await context.close()
        await browser.close()


async def test_imports_a_har_recorded_by_playwright(tmp_path, isolated, fixture_site, hits,
                                                    described):
    har = tmp_path / "e2e.har"
    await _record_har(fixture_site, har)
    assert b"sid=abc123" in har.read_bytes()
    hits.clear()

    result = await pipeline.import_har(har)

    assert sum(hits.values()) == 0
    effects = {(e["method"], urlsplit(e["url"]).path): e["effect"] for e in result["endpoints"]}
    assert effects[("GET", "/api/items")] == "read"
    assert effects[("GET", "/api/search")] == "read"
    assert effects[("POST", "/api/notes")] == "write"
    assert ("GET", "/api/recommendations") in effects
    for path in (p for p in isolated.rglob("*") if p.is_file()):
        assert b"abc123" not in path.read_bytes(), path
    assert "abc123" not in json.dumps(described)
    assert get_cookies(urlsplit(fixture_site).netloc) is None


def test_import_har_saves_the_skill_when_the_llm_is_unreachable(tmp_path, monkeypatch):
    async def unreachable(endpoints):
        raise LLMError("ConnectError: All connection attempts failed")

    monkeypatch.setattr(pipeline, "describe_endpoints", unreachable)
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>"),
        _entry("GET", "https://app.local/static/app.js", mime="application/javascript",
               body=APP_JS),
        _entry("GET", "https://app.local/api/items", body='{"items": []}'),
    ])

    result = CliRunner().invoke(main, ["import-har", str(har)])

    assert result.exit_code == 0, result.output
    descriptions = {urlsplit(e["url"]).path: e["description"]
                    for e in json.loads(result.stdout)["endpoints"]}
    assert descriptions == {
        "/api/items": "(no description)",
        "/api/recommendations": "JS bundle route: /api/recommendations",
    }


async def test_import_leaves_the_har_untouched_and_skips_aborted_and_preflight_entries(
        tmp_path, described):
    har = _write(tmp_path, [
        _entry("GET", "https://app.local/", mime="text/html", body="<html></html>"),
        _entry("OPTIONS", "https://app.local/api/items", status=204, mime=""),
        _entry("GET", "https://app.local/api/items", body='{"items": []}'),
        _entry("GET", "https://app.local/api/aborted", status=-1),
        _entry("HEAD", "https://app.local/api/items/head", mime=""),
    ])
    before = har.read_bytes()

    result = await pipeline.import_har(har)

    assert har.read_bytes() == before
    assert [(e["method"], urlsplit(e["url"]).path) for e in result["endpoints"]] == [
        ("GET", "/api/items")]
