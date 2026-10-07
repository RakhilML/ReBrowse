from __future__ import annotations

import ast
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from click.testing import CliRunner, Result
from test_har import _entry, _json_post, _write
from test_mock import _app

from rebrowse import config, contract
from rebrowse.auth.vault import store_api_key, store_cookies
from rebrowse.capture.har import load_har
from rebrowse.capture.store import save_capture
from rebrowse.cli import main
from rebrowse.execution import executor
from rebrowse.mock import MockServer, build_routes
from rebrowse.models import CaptureResult, RawRequest

SITE = "http://app.local"
ITEMS = {"items": [{"id": 0}], "next": "x"}
RESULTS = {"results": [{"id": 1, "title": "t"}]}


def _page() -> dict:
    return _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")


def _get(path: str, payload, **kwargs) -> dict:
    url = path if "://" in path else f"{SITE}{path}"
    return _entry("GET", url, body=json.dumps(payload), **kwargs)


def _run(*args: str) -> Result:
    return CliRunner().invoke(main, ["contract", *args])


def _invoke(*args: str) -> tuple[int, dict]:
    result = _run(*args)
    return result.exit_code, json.loads(result.stdout)


def _contract(tmp_path: Path, entries: list, target: str) -> tuple[int, dict]:
    return _invoke(str(_write(tmp_path, [_page(), *entries])), "--against", target)


def _rows(report: dict) -> list[tuple]:
    return [(c["kind"], c["route"], c.get("field"), c.get("base"), c.get("head"))
            for c in report["changes"]]


@contextmanager
def _running(server: ThreadingHTTPServer) -> Iterator[str]:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


class _SlowSearch(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/search":
            time.sleep(1)
            return
        body = json.dumps(ITEMS).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Scripted(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        status, content_type, body = self.server.answers[self.path]
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _scripted(answers: dict[str, tuple[int, str, bytes]]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Scripted)
    server.answers = answers
    return server


@pytest.mark.parametrize("text, origin", [
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("https://staging.example.com/", "https://staging.example.com"),
    ("HTTP://Staging.Example.com:8443", "http://staging.example.com:8443"),
    ("http://[::1]:8000/", "http://[::1]:8000"),
])
def test_targets_are_normalized_origins(text, origin):
    assert contract.parse_target(text) == origin


@pytest.mark.parametrize("text", [
    "ftp://x", "http://x/api", "http://x?a=1", "http://x#top", "http://u:p@x", "localhost:8000",
    "app.local", "http://x:99999",
])
def test_anything_but_an_origin_is_rejected(tmp_path, text):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS)])

    with pytest.raises(ValueError):
        contract.parse_target(text)
    result = _run(str(har), "--against", text)

    assert result.exit_code == 2 and "--against" in result.stderr


def test_against_is_required(tmp_path):
    result = _run(str(_write(tmp_path, [_page(), _get("/api/items", ITEMS)])))

    assert result.exit_code == 2 and "--against" in result.stderr


def test_replays_drop_recorded_credentials_and_browser_context():
    body = {"operationName": "Items", "query": "query Items { items { id } }",
            "variables": {"token": "SECRET", "page": 2}}
    req = RawRequest(
        url=f"{SITE}/api/items?page=2&access_token=SECRET#frag", method="POST",
        request_headers={
            "Cookie": "sid=SECRET", "Authorization": "Bearer SECRET", "X-CSRF-Token": "SECRET",
            "X-Session-Id": "SECRET", "Origin": SITE, "Referer": f"{SITE}/app",
            "If-None-Match": 'W/"SECRET"', "X-Forwarded-User": "Bearer SECRET",
            "X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Host": "app.local",
            "X-User": "Jürgen",
        },
        request_body=json.dumps(body), response_status=200)

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:9/api/items?page=2&access_token=%3Credacted%3E"
    assert dict(request.headers) == {
        "host": "127.0.0.1:9", "x-requested-with": "XMLHttpRequest",
        "accept": "application/json", "content-type": "application/json",
        "user-agent": config.REBROWSE_UA, "content-length": str(len(request.content)),
    }
    assert b"SECRET" not in request.content
    assert json.loads(request.content)["variables"] == {"token": "<redacted>", "page": 2}


def test_gets_send_no_body_and_keep_the_recorded_query():
    req = RawRequest(url=f"{SITE}/api/search?q=a%20b&page=2", method="GET",
                     request_body="ignored", response_status=200)

    request = contract.replay_request(req, "https://staging.example.com")

    assert str(request.url) == "https://staging.example.com/api/search?q=a%20b&page=2"
    assert request.content == b""


def test_secret_path_parameters_are_removed():
    req = RawRequest(url=f"{SITE}/api/echo;jsessionid=JSESSION_SECRET;v=2?page=1",
                     method="GET", response_status=200)

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert str(request.url) == "http://127.0.0.1:9/api/echo;v=2?page=1"


def test_headers_that_cannot_be_sent_as_recorded_are_dropped():
    req = RawRequest(url=f"{SITE}/api/items", method="GET", response_status=200,
                     request_headers={
                         "X-Ünicode": "1", "X Space": "1", "X-Trace": "a\r\nX-Injected: SECRET",
                         "X-Tab": "a\tb", "X-Padded": "  kept ", "Accept": "application/json"})

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert dict(request.headers) == {
        "host": "127.0.0.1:9", "x-padded": "kept", "accept": "application/json",
        "user-agent": config.REBROWSE_UA}


def test_only_a_key_stored_for_the_target_host_is_attached():
    req = RawRequest(url=f"{SITE}/api/items?page=2", method="GET", response_status=200)
    store_api_key("app.local", "k2")
    store_cookies("app.local", [{"name": "sid", "value": "c1", "domain": "app.local"}])
    store_cookies("127.0.0.1", [{"name": "sid", "value": "c2", "domain": "127.0.0.1"}])

    unkeyed = contract.replay_request(req, "http://127.0.0.1:9")
    store_api_key("127.0.0.1", "k1")
    bearer = contract.replay_request(req, "http://127.0.0.1:9")
    store_api_key("127.0.0.1", "k1", auth_type="query")
    query = contract.replay_request(req, "http://127.0.0.1:9")

    assert not {"authorization", "x-api-key"} & unkeyed.headers.keys()
    assert str(unkeyed.url) == "http://127.0.0.1:9/api/items?page=2"
    assert bearer.headers["authorization"] == "Bearer k1"
    assert str(query.url) == "http://127.0.0.1:9/api/items?page=2&api_key=k1"
    assert "authorization" not in query.headers
    assert all("cookie" not in r.headers for r in (unkeyed, bearer, query))


def test_a_compatible_server_passes(tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [
        _get("/api/items", ITEMS), _get("/api/search?q=a", RESULTS)], fixture_site)

    assert code == 0
    assert report == {
        "source": str((tmp_path / "session.har").resolve()), "domain": "app.local",
        "target": fixture_site, "routes": 2, "replayed": 2, "answered": 2, "skipped": [],
        "breaking": 0, "changes": [],
    }
    assert hits[("GET", "/api/items")] == hits[("GET", "/api/search")] == 1


def test_answers_that_break_the_client_fail_the_run(tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [
        _get("/api/search", {**RESULTS, "total": 3}),
        _get("/api/fail", {"ok": True}),
        _get("/moved", ITEMS),
        _get("/blocked", {"ok": True}),
    ], fixture_site)

    assert code == 1
    assert (report["breaking"], report["replayed"], report["answered"]) == (4, 4, 4)
    assert _rows(report) == [
        ("status", "GET /api/fail", None, [200], [503]),
        ("field_removed", "GET /api/search", "$.total", ["integer"], None),
        ("blocked", "GET /blocked", None, None, None),
        ("status", "GET /moved", None, [200], [302]),
    ]
    blocked = report["changes"][2]
    assert "just a moment" in blocked["error"] and blocked["failed"] == 1
    assert hits[("GET", "/moved")] == 1 and hits[("GET", "/api/items")] == 0


def test_only_reads_are_sent(tmp_path, fixture_site, hits):
    mutation = {"operationName": "AddNote", "query": "mutation AddNote { addNote { id } }"}
    query = {"operationName": "Feed", "query": "query Feed { feed { id } }"}
    echo = {"method": "POST", "received": "", "content_type": None}

    code, report = _contract(tmp_path, [
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body='{"id": 1}'),
        _get("/api/comments/delete?id=1", {"ok": True}),
        _entry("POST", f"{SITE}/graphql", post=_json_post(mutation), body='{"data": {}}'),
        _entry("POST", f"{SITE}/graphql", post=_json_post(query), body=json.dumps(echo)),
    ], fixture_site)

    assert hits[("POST", "/api/notes")] == hits[("GET", "/api/comments/delete")] == 0
    assert hits[("POST", "/graphql")] == 1
    assert report["skipped"] == [
        {"route": "POST /api/notes", "reason": "write"},
        {"route": "GET /api/comments/delete", "reason": "destructive"},
        {"route": "POST /graphql [GraphQL mutation: AddNote]", "reason": "write"},
    ]
    assert (code, report["routes"], report["changes"]) == (0, 1, [])


def test_each_route_gets_at_most_three_distinct_replays(tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [
        *(_get("/api/items", ITEMS) for _ in range(5)),
        *(_get(f"/api/users/{n}", {"id": n}) for n in range(101, 106)),
        _entry("GET", f"{SITE}/api/feed", status=304, mime=""),
        _get("http://api.app.local/api/x", {"ok": True}),
    ], fixture_site)

    assert hits[("GET", "/api/items")] == 1
    assert [hits[("GET", f"/api/users/{n}")] for n in range(101, 106)] == [1, 1, 1, 0, 0]
    assert hits[("GET", "/api/feed")] == hits[("GET", "/api/x")] == 0
    assert (report["routes"], report["replayed"], report["answered"]) == (2, 4, 4)
    assert report["skipped"] == [
        {"route": "GET /api/feed", "reason": "not modified"},
        {"route": "GET api.app.local/api/x", "reason": "other host"},
    ]
    assert code == 1
    assert _rows(report) == [("status", "GET /api/users/{users_id}", None, [200], [404])]


def test_recordings_that_cannot_be_sent_are_skipped(tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [
        _get("/api/items", ITEMS, headers=[
            ("X-Ünicode", "1"), ("X-Trace", "a\r\nX-Injected: HEADER_SECRET")]),
        _get("/api/search?q=a\x01b", RESULTS),
        _get("/api/search?q=a", RESULTS),
        _get("/api/echo?q=" + "a" * 70_000, {"ok": True}),
    ], fixture_site)

    assert (code, report["replayed"], report["answered"], report["changes"]) == (0, 2, 2, [])
    assert report["skipped"] == [{"route": "GET /api/echo", "reason": "unreplayable"}]
    assert hits[("GET", "/api/items")] == hits[("GET", "/api/search")] == 1
    assert hits[("GET", "/api/echo")] == 0


def test_a_request_that_stopped_answering_breaks_its_route(tmp_path):
    challenge = b"<html><title>Just a moment...</title></html>"
    with _running(_scripted({
        "/api/orders/101": (200, "application/json", b'{"id": 101}'),
        "/api/orders/102": (302, "text/html", b""),
        "/api/books/11": (404, "application/json", b'{"error": "gone"}'),
        "/api/books/12": (200, "application/json", b'{"id": 12}'),
        "/api/carts/101": (403, "text/html", challenge),
        "/api/carts/102": (404, "application/json", b'{"error": "none"}'),
    })) as origin:
        code, report = _contract(tmp_path, [
            _get("/api/orders/101", {"id": 101}), _get("/api/orders/102", {"id": 102}),
            _get("/api/books/11", {"id": 1}), _get("/api/books/12", {"error": "x"}, status=404),
            _get("/api/carts/101", {"id": 1}), _get("/api/carts/102", {"error": "x"}, status=404),
        ], origin)

    assert code == 1
    assert _rows(report) == [
        ("status", "GET /api/books/{books_id}", None, [200], [404]),
        ("blocked", "GET /api/carts/{carts_id}", None, None, None),
        ("status", "GET /api/orders/{orders_id}", None, [200], [302]),
    ]
    assert report["breaking"] == 3 and report["changes"][1]["failed"] == 1


def test_json_at_the_root_answered_with_html_is_a_failed_replay(tmp_path, fixture_site):
    code, report = _invoke(str(_write(tmp_path, [_get("/", {"version": 1})])),
                           "--against", fixture_site)

    assert code == 1
    assert report["changes"] == [{
        "severity": "breaking", "kind": "no_response", "route": "GET /",
        "error": "answered with a non-API response", "failed": 1,
    }]


def test_a_server_that_never_answers_is_an_input_error(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    code, report = _contract(tmp_path, [_get("/api/items", ITEMS)], f"http://127.0.0.1:{port}")

    assert code == 2
    assert report["error"].startswith(f"No response from http://127.0.0.1:{port}: Connect")


def test_a_replay_that_times_out_fails_its_route(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "TIMEOUT_S", 0.2)

    with _running(ThreadingHTTPServer(("127.0.0.1", 0), _SlowSearch)) as origin:
        code, report = _contract(tmp_path, [
            _get("/api/items", ITEMS), _get("/api/search", RESULTS)], origin)

    assert code == 1
    assert (report["replayed"], report["answered"]) == (2, 1)
    [change] = report["changes"]
    assert change["error"].startswith("ReadTimeout")
    assert {**change, "error": None} == {"severity": "breaking", "kind": "no_response",
                                          "route": "GET /api/search", "error": None, "failed": 1}


def test_credentials_in_a_saved_capture_never_reach_the_target(tmp_path, fixture_site):
    echo = {"method": "GET", "cookie": None, "user_agent": "Mozilla/5.0", "authorization": None,
            "query": ""}
    capture = CaptureResult(
        domain=urlsplit(fixture_site).netloc, final_url=f"{fixture_site}/",
        requests=[RawRequest(
            url=f"{fixture_site}/api/echo", method="GET",
            request_headers={"cookie": "sid=COOKIE_SECRET",
                             "authorization": "Bearer BEARER_SECRET"},
            response_status=200, response_headers={"content-type": "application/json"},
            response_body=json.dumps(echo))])
    path = save_capture(capture, tmp_path / "capture.json")

    code, report = _invoke(str(path), "--against", fixture_site)

    assert (code, report["answered"], report["changes"]) == (0, 1, [])


def test_a_recording_is_compatible_with_its_own_mock(tmp_path):
    har = _write(tmp_path, _app())

    with _running(MockServer(build_routes(load_har(har)), 0)) as origin:
        code, report = _invoke(str(har), "--against", origin)

    assert (code, report["breaking"]) == (0, 0)
    assert report["answered"] == report["replayed"] > 0
    assert {"route": "POST /api/notes", "reason": "write"} in report["skipped"]
    assert {"route": "POST /api/search", "reason": "write"} in report["skipped"]


def test_the_report_holds_no_recorded_or_live_values(tmp_path, fixture_site):
    recorded = {"method": "sku-SECRET-1", "cookie": "sku-SECRET-2", "user_agent": "sku-SECRET-3",
                "authorization": None, "query": "", "promo": "sku-SECRET-4"}

    result = _run(str(_write(tmp_path, [_page(), _get("/api/echo?q=sku-SECRET-5", recorded)])),
                  "--against", fixture_site)

    assert result.exit_code == 1
    assert _rows(json.loads(result.stdout)) == [
        ("field_type", "GET /api/echo", "$.cookie", ["string"], ["null"]),
        ("field_removed", "GET /api/echo", "$.promo", ["string"], None),
    ]
    assert "sku-SECRET" not in result.stdout and config.REBROWSE_UA not in result.stdout


def test_the_report_is_deterministic(tmp_path, fixture_site):
    har = _write(tmp_path, [_page(), _get("/api/search", {**RESULTS, "total": 3}),
                            _get("/blocked", {}), _get("/api/fail", {}), _get("/moved", {})])

    first, second = (_run(str(har), "--against", fixture_site) for _ in range(2))

    assert first.exit_code == second.exit_code == 1
    assert first.stdout == second.stdout


def test_nothing_to_replay_is_an_input_error(tmp_path, fixture_site, hits):
    writes = _write(tmp_path, [_page(), _entry(
        "POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body="{}")], "writes.har")
    telemetry = _write(tmp_path, [_page(), _get("/collect", {}), _get("/api/metrics", {})],
                       "telemetry.har")
    broken = tmp_path / "broken.har"
    broken.write_text("not json", encoding="utf-8")

    for source in (writes, telemetry):
        code, report = _invoke(str(source), "--against", fixture_site)
        assert code == 2 and "app.local" in report["error"], source
    for source in (str(broken), "nowhere.test"):
        code, report = _invoke(source, "--against", fixture_site)
        assert code == 2 and "error" in report, source
    assert sum(hits.values()) == 0


def test_group_help_lists_contract():
    assert ("contract <source> replay recorded reads against a server, report breaking changes"
            in CliRunner().invoke(main, ["--help"]).output)


def test_contract_never_executes_endpoints_or_opens_a_browser():
    tree = ast.parse(Path(contract.__file__).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    modules |= {node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}

    assert "execute_endpoint" not in names
    assert not any(module.split(".")[0] == "playwright" for module in modules)


def test_a_graphql_mutation_sent_over_get_is_never_replayed(tmp_path, fixture_site, hits):
    mutation = urlencode({"operationName": "DeleteUser",
                          "query": "mutation DeleteUser { deleteUser(id: 1) { ok } }"})

    code, report = _contract(tmp_path, [
        _get("/api/items", ITEMS), _get(f"/graphql?{mutation}", {"data": {}})], fixture_site)

    assert hits[("GET", "/graphql")] == 0
    assert [row["route"] for row in report["skipped"]] == [
        "GET /graphql [GraphQL mutation: DeleteUser]"]
    assert (code, report["replayed"]) == (0, 1)


def test_a_query_key_for_the_target_replaces_the_recorded_one():
    store_api_key("127.0.0.1", "k1", auth_type="query")
    req = RawRequest(url=f"{SITE}/api/maps?q=x&api_key=RECORDED", method="GET",
                     response_status=200)

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert parse_qs(request.url.query.decode()) == {"q": ["x"], "api_key": ["k1"]}


def test_only_targets_off_loopback_are_paced(tmp_path, fixture_site, monkeypatch):
    paced: list[str] = []

    async def pace(host: str) -> None:
        paced.append(host)

    monkeypatch.setattr(executor, "pace", pace)
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS), _get("/api/search", RESULTS)])

    assert _invoke(str(har), "--against", fixture_site)[0] == 0
    assert paced == []
    monkeypatch.setattr(contract, "LOOPBACK", frozenset())
    assert _invoke(str(har), "--against", fixture_site)[0] == 0
    assert paced == ["127.0.0.1", "127.0.0.1"]


def test_domain_picks_the_host_under_test_in_a_har(tmp_path, fixture_site, hits):
    har = _write(tmp_path, [_page(), _get("http://api.app.local/api/items", ITEMS),
                            _get("/api/search", RESULTS)])

    code, report = _invoke(str(har), "--against", fixture_site, "-d", "api.app.local")

    assert (code, report["domain"], report["routes"]) == (0, "api.app.local", 1)
    assert report["skipped"] == [{"route": "GET app.local/api/search", "reason": "other host"}]
    assert hits[("GET", "/api/items")] == 1 and hits[("GET", "/api/search")] == 0


def test_a_target_that_only_answers_with_challenges_fails_rather_than_errors(
        tmp_path, fixture_site):
    code, report = _contract(tmp_path, [
        _get("/blocked?page=1", {"ok": True}), _get("/blocked?page=2", {"ok": True})],
        fixture_site)

    assert code == 1
    assert (report["replayed"], report["answered"], report["breaking"]) == (2, 2, 1)
    [change] = report["changes"]
    assert (change["kind"], change["failed"]) == ("blocked", 2)


def test_additive_changes_are_reported_without_failing(tmp_path, fixture_site):
    code, report = _contract(tmp_path, [_get("/api/items", {"items": [{"id": 0}]})],
                             fixture_site)

    assert (code, report["breaking"]) == (0, 0)
    assert _rows(report) == [("field_added", "GET /api/items", "$.next", None, ["string"])]
    assert report["changes"][0]["severity"] == "info"


class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self.server.seen.append({
            "method": self.command, "path": self.path, "cookie": self.headers.get("cookie"),
            "content_type": self.headers.get("content-type"), "body": body.decode()})
        payload = json.dumps(ITEMS).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("set-cookie", "sid=FROM_TARGET; Path=/")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer


def test_cookies_set_by_the_target_are_never_sent_back(tmp_path):
    feed = {"operationName": "Feed", "query": "query Feed { feed { id } }",
            "variables": {"authToken": "VAR_SECRET"}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen = []

    with _running(server) as origin:
        code, report = _contract(tmp_path, [
            _get("/api/items", ITEMS), _get("/api/items?page=2", ITEMS),
            _entry("POST", f"{SITE}/graphql", post=_json_post(feed), body=json.dumps(ITEMS),
                   headers=[("Content-Type", "application/json")]),
        ], origin)

    assert (code, report["replayed"], report["changes"]) == (0, 3, [])
    assert [(seen["method"], seen["path"], seen["cookie"]) for seen in server.seen] == [
        ("GET", "/api/items", None), ("GET", "/api/items?page=2", None),
        ("POST", "/graphql", None)]
    graphql = server.seen[2]
    assert graphql["content_type"] == "application/json"
    assert json.loads(graphql["body"]) == {**feed, "variables": {"authToken": "<redacted>"}}


def test_a_session_id_in_a_saved_capture_url_never_reaches_the_target(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen = []

    with _running(server) as origin:
        capture = CaptureResult(
            domain=urlsplit(origin).netloc, final_url=f"{origin}/", requests=[RawRequest(
                url=f"{origin}/api/items;jsessionid=JSESSION_SECRET?page=1", method="GET",
                response_status=200, response_headers={"content-type": "application/json"},
                response_body=json.dumps(ITEMS))])
        path = save_capture(capture, tmp_path / "capture.json")
        code, report = _invoke(str(path), "--against", origin)

    assert (code, report["answered"], report["changes"]) == (0, 1, [])
    assert [seen["path"] for seen in server.seen] == ["/api/items?page=1"]


def test_a_key_for_the_target_never_follows_a_redirect(tmp_path, fixture_site, hits):
    store_api_key("127.0.0.1", "k1")
    store_api_key("localhost", "k2")

    code, report = _contract(tmp_path, [_get("/sso", {"ok": True})], fixture_site)

    assert code == 1
    assert _rows(report) == [("status", "GET /sso", None, [200], [302])]
    assert hits[("GET", "/sso")] == 1 and hits[("GET", "/login")] == 0


def test_failed_replays_are_never_retried(tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [_get("/api/fail", {"ok": True})], fixture_site)

    assert code == 1
    assert _rows(report) == [("status", "GET /api/fail", None, [200], [503])]
    assert hits[("GET", "/api/fail")] == 1


def test_a_json_route_answered_with_an_html_page_breaks_its_content_type(
        tmp_path, fixture_site):
    code, report = _contract(tmp_path, [_get("/big", {"ok": True})], fixture_site)

    assert code == 1
    assert _rows(report) == [
        ("content_type", "GET /big", None, ["application/json"], ["text/html"])]


def test_a_saved_capture_is_found_by_its_host(fixture_site):
    netloc = urlsplit(fixture_site).netloc
    capture = CaptureResult(domain=netloc, final_url=f"{fixture_site}/", requests=[RawRequest(
        url=f"{fixture_site}/api/items?page=2", method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body=json.dumps(ITEMS))])
    path = save_capture(capture)

    code, report = _invoke(netloc, "--against", fixture_site)

    assert (code, report["source"], report["domain"]) == (0, str(path.resolve()), netloc)
    assert (report["answered"], report["changes"]) == (1, [])


def test_graphql_variables_in_a_get_query_are_redacted():
    variables = json.dumps({"authToken": "SECRET", "first": 2})
    query = urlencode({"query": "query Viewer { viewer { id } }", "variables": variables})
    req = RawRequest(url=f"{SITE}/graphql?{query}", method="GET", response_status=200)

    request = contract.replay_request(req, "http://127.0.0.1:9")

    sent = parse_qs(request.url.query.decode())
    assert json.loads(sent["variables"][0]) == {"authToken": "<redacted>", "first": 2}
    assert "SECRET" not in str(request.url)


def test_a_live_body_over_the_size_limit_is_not_judged(fixture_site, monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 10)
    capture = CaptureResult(domain="app.local", final_url=f"{SITE}/", requests=[RawRequest(
        url=f"{SITE}/api/search", method="GET", response_status=200,
        response_headers={"content-type": "application/json"},
        response_body=json.dumps({**RESULTS, "total": 3}))])

    code, report = _invoke(str(save_capture(capture)), "--against", fixture_site)

    assert (code, report["answered"], report["changes"]) == (0, 1, [])


def test_recorded_transport_headers_are_not_forwarded():
    body = {"operationName": "Feed", "query": "query Feed { feed { id } }",
            "variables": {"password": "A_LONG_RECORDED_PASSWORD"}}
    req = RawRequest(
        url=f"{SITE}/graphql", method="POST", response_status=200, request_body=json.dumps(body),
        request_headers={
            "Content-Type": "application/json", "Content-Length": "999", "Connection": "close",
            "Accept-Encoding": "gzip, deflate, br, zstd", "Transfer-Encoding": "chunked",
            ":authority": "app.local", "Proxy-Authorization": "Basic cHJveHk="})

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert dict(request.headers) == {
        "host": "127.0.0.1:9", "content-type": "application/json",
        "user-agent": config.REBROWSE_UA, "content-length": str(len(request.content))}
    assert b"A_LONG_RECORDED_PASSWORD" not in request.content


def test_a_header_key_for_the_target_replaces_the_recorded_one():
    store_api_key("127.0.0.1", "k1", auth_type="header")
    store_api_key("localhost", "k2")
    req = RawRequest(url=f"{SITE}/api/items", method="GET", response_status=200,
                     request_headers={"X-API-Key": "RECORDED", "Accept": "application/json"})

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert request.headers.get_list("x-api-key") == ["k1"]
    assert "authorization" not in request.headers


def test_a_graphql_query_sent_over_get_is_replayed(tmp_path):
    query = urlencode({"operationName": "Feed", "query": "query Feed { feed { id } }"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen = []

    with _running(server) as origin:
        code, report = _contract(tmp_path, [_get(f"/graphql?{query}", ITEMS)], origin)

    assert (code, report["replayed"], report["skipped"], report["changes"]) == (0, 1, [], [])
    assert [(seen["method"], seen["path"]) for seen in server.seen] == [
        ("GET", f"/graphql?{query}")]


def test_graphql_posts_without_query_text_are_never_replayed(tmp_path, fixture_site, hits):
    persisted = {"operationName": "Feed",
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "ab12"}}}
    batch = [{"operationName": "Feed", "query": "query Feed { feed { id } }"}]

    code, report = _contract(tmp_path, [
        _get("/api/items", ITEMS),
        _entry("POST", f"{SITE}/graphql", post=_json_post(persisted), body='{"data": {}}'),
        _entry("POST", f"{SITE}/gql", post=_json_post(batch), body='[{"data": {}}]'),
    ], fixture_site)

    assert hits[("POST", "/graphql")] == hits[("POST", "/gql")] == 0
    assert [row["reason"] for row in report["skipped"]] == ["write", "write"]
    assert (code, report["replayed"]) == (0, 1)


def test_a_route_recorded_as_a_redirect_that_still_redirects_passes(
        tmp_path, fixture_site, hits):
    code, report = _contract(tmp_path, [
        _entry("GET", f"{SITE}/moved", status=302, mime="text/html", body="")], fixture_site)

    assert (code, report["answered"], report["changes"]) == (0, 1, [])
    assert hits[("GET", "/moved")] == 1 and hits[("GET", "/api/items")] == 0


class _Stream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        if self.path == "/api/items":
            body = json.dumps(ITEMS).encode()
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("content-type", "application/json")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass


def test_a_never_ending_answer_fails_its_route_within_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "TIMEOUT_S", 0.3)
    started = time.monotonic()

    with _running(ThreadingHTTPServer(("127.0.0.1", 0), _Stream)) as origin:
        code, report = _contract(tmp_path, [_get("/api/items", ITEMS), _get("/api/feed", ITEMS)],
                                 origin)

    assert time.monotonic() - started < 5
    assert code == 1
    [change] = report["changes"]
    assert (change["kind"], change["route"]) == ("no_response", "GET /api/feed")
    assert change["error"].startswith("TimeoutError")


def test_streams_mutations_actions_and_tokens_in_paths_are_never_sent(
        tmp_path, fixture_site, hits):
    mixed = {"operationName": "DeleteAll",
             "query": "query Feed { feed { id } }\nmutation DeleteAll { deleteAll { ok } }"}
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    code, report = _contract(tmp_path, [
        _get("/api/items", ITEMS),
        _entry("GET", f"{SITE}/api/events", mime="text/event-stream", body="data: 1\n\n"),
        _entry("POST", f"{SITE}/graphql", post=_json_post(mixed), body='{"data": {}}'),
        _get("/ajax.php?action=delete&id=5", {"ok": True}),
        _get(f"/api/invites/{jwt}", {"ok": True}),
    ], fixture_site)

    assert code == 0
    assert {row["reason"] for row in report["skipped"]} == {
        "stream", "destructive", "credential in path"}
    assert {path for _, path in hits} == {"/api/items"}


def test_a_route_that_vanishes_from_the_answers_is_breaking(tmp_path):
    deep = b"[" * 5000 + b"]" * 5000
    with _running(_scripted({
        "/api/items": (200, "application/json", json.dumps(ITEMS).encode()),
        "/api/gone": (200, "application/json", deep),
    })) as origin:
        code, report = _contract(tmp_path, [_get("/api/items", ITEMS),
                                            _get("/api/gone", {"ok": True})], origin)

    assert code == 1
    assert [(c["kind"], c["route"], c["failed"]) for c in report["changes"]] == [
        ("no_response", "GET /api/gone", 1)]


def test_a_new_server_error_after_a_recorded_404_is_breaking(tmp_path):
    with _running(_scripted({
        "/api/items": (200, "application/json", json.dumps(ITEMS).encode()),
        "/api/books/11": (500, "application/json", b'{"error": "boom"}'),
    })) as origin:
        code, report = _contract(tmp_path, [
            _get("/api/items", ITEMS), _get("/api/books/11", {"error": "x"}, status=404)], origin)

    assert code == 1
    assert _rows(report) == [("status", "GET /api/books/{books_id}", None, [404], [500])]


def test_a_saved_capture_can_be_tested_on_a_sibling_host(fixture_site):
    capture = CaptureResult(domain="www.app.test", final_url="https://www.app.test/",
                            requests=[RawRequest(
                                url="https://api.app.test/api/items", method="GET",
                                response_status=200,
                                response_headers={"content-type": "application/json"},
                                response_body=json.dumps(ITEMS))])
    path = str(save_capture(capture))

    code, report = _invoke(path, "--against", fixture_site)
    moved_code, moved = _invoke(path, "--against", fixture_site, "-d", "api.app.test")

    assert code == 2 and "1 other host; pass --domain" in report["error"]
    assert (moved_code, moved["domain"], moved["replayed"]) == (0, "api.app.test", 1)


@pytest.mark.parametrize("first, then", [
    ({"items": [{"id": 1, "text": "a"}]}, {"items": []}),
    ({"items": []}, {"items": [{"id": 1, "text": "a"}]}),
], ids=["full then empty", "empty then full"])
def test_a_request_recorded_twice_is_judged_against_both_answers(tmp_path, first, then):
    with _running(_scripted({
        "/api/notes": (200, "application/json", json.dumps({"items": [{"id": 1}]}).encode()),
    })) as origin:
        code, report = _contract(tmp_path, [_get("/api/notes", first), _get("/api/notes", then)],
                                 origin)

    assert (code, report["replayed"], report["answered"]) == (1, 1, 1)
    assert _rows(report) == [
        ("field_removed", "GET /api/notes", "$.items[].text", ["string"], None)]


def test_a_status_already_recorded_for_the_request_never_breaks(tmp_path):
    with _running(_scripted({
        "/api/me": (401, "application/json", b'{"error": "sign in"}'),
        "/api/orders/7": (302, "text/html", b""),
    })) as origin:
        code, report = _contract(tmp_path, [
            _get("/api/me", {"error": "x"}, status=401), _get("/api/me", {"id": 1}),
            _get("/api/orders/7", {"error": "x"}, status=404), _get("/api/orders/7", {"id": 7}),
        ], origin)

    assert code == 1
    assert [(c["severity"], c["route"], c["base"], c["head"]) for c in report["changes"]] == [
        ("info", "GET /api/me", [200, 401], [401]),
        ("breaking", "GET /api/orders/7", [200, 404], [302]),
    ]


def test_a_request_recorded_with_other_headers_is_sent_alike_in_any_order():
    recordings = [
        RawRequest(url=f"{SITE}/api/items", method="GET", response_status=200,
                   request_headers={"Accept": accept}, response_body=json.dumps(ITEMS),
                   response_headers={"content-type": "application/json"})
        for accept in ("application/json", "*/*")]

    sent = [contract.plan(CaptureResult(domain="app.local", final_url=f"{SITE}/",
                                        requests=requests), "http://127.0.0.1:9")[0]
            for requests in (recordings, recordings[::-1])]

    assert [[replay.request.headers["accept"] for replay in replays] for replays in sent] == [
        ["*/*"], ["*/*"]]


def test_tracing_headers_and_cache_busters_are_not_sent():
    req = RawRequest(
        url=f"{SITE}/api/items?page=2&_=1759740000000&q=a%20b", method="GET", response_status=200,
        request_headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "congo=t61rcWkgMzE", "sentry-trace": "4bf92f35-00f067aa-1",
            "baggage": "sentry-user_id=ann%40corp.test,sentry-environment=prod",
            "X-Request-Id": "r-1", "X-Correlation-Id": "c-1", "X-Amzn-Trace-Id": "Root=1-a",
            "X-B3-TraceId": "4bf92f35", "X-Datadog-Trace-Id": "123", "newrelic": "eyJ2Ijpb",
            "X-Cloud-Trace-Context": "105445aa7843bc8bf206b1/1", "Accept": "application/json"})

    request = contract.replay_request(req, "http://127.0.0.1:9")

    assert str(request.url) == "http://127.0.0.1:9/api/items?page=2&q=a%20b"
    assert dict(request.headers) == {
        "host": "127.0.0.1:9", "accept": "application/json", "user-agent": config.REBROWSE_UA}


def test_secret_named_graphql_arguments_are_sent_redacted():
    document = 'query User { user(id: 1001, token: "SECRET") { id } }'
    post = RawRequest(url=f"{SITE}/graphql", method="POST", response_status=200,
                      request_headers={"Content-Type": "application/json"},
                      request_body=json.dumps({"operationName": "User", "query": document}))
    get = RawRequest(url=f"{SITE}/graphql?{urlencode({'query': document})}", method="GET",
                     response_status=200)

    sent_post, sent_get = (contract.replay_request(req, "http://127.0.0.1:9")
                           for req in (post, get))

    redacted = 'query User { user(id: 1001, token: "<redacted>") { id } }'
    assert json.loads(sent_post.content)["query"] == redacted
    assert parse_qs(sent_get.url.query.decode())["query"] == [redacted]
