from __future__ import annotations

import ast
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from click.testing import CliRunner
from test_har import _entry, _json_post, _write

from rebrowse import config
from rebrowse import mock as mock_module
from rebrowse.capture.store import latest_capture, load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.mock import MockServer, build_routes, route_table
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.safety import REDACTED

SITE = "https://app.example.com"
PERSISTED = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "ab" * 32}})
HTTP_CLIENTS = {"httpx", "requests", "aiohttp", "urllib.request", "http.client", "socket",
                "rebrowse.net", "rebrowse.execution.executor"}


def _gql(name: str) -> dict:
    return _json_post({"operationName": name, "query": f"query {name} {{ {name.lower()} {{ id }} }}"})


def _app() -> list[dict]:
    return [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html><body>app</body></html>"),
        _entry("GET", f"{SITE}/api/users/1001", body=json.dumps({"id": 1001, "name": "ann"})),
        _entry("GET", f"{SITE}/api/users/42", body=json.dumps({"id": 42, "name": "bob"})),
        _entry("GET", f"{SITE}/api/users/me", body=json.dumps({"id": 7, "name": "me"})),
        _entry("GET", f"{SITE}/api/items?sort=name&size=20", body='{"sort": "name"}'),
        _entry("GET", f"{SITE}/api/items?page=1", body='{"page": 1}'),
        _entry("GET", f"{SITE}/api/items?page=2&size=20", body='{"page": 2}'),
        _entry("POST", f"{SITE}/api/search", post=_json_post({"q": "cats"}), body='{"hits": 1}'),
        _entry("POST", f"{SITE}/api/search", post=_json_post({"q": "dogs"}), body='{"hits": 2}'),
        _entry("POST", f"{SITE}/graphql", post=_gql("GetUser"), body='{"data": {"user": {"id": 1}}}'),
        _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": []}}'),
        _entry("GET", f"{SITE}/graphql?operationName=Viewer&extensions={quote(PERSISTED)}",
               body='{"data": {"viewer": {"id": 7}}}'),
        _entry("POST", f"{SITE}/api/notes", status=201, post=_json_post({"text": "hi"}),
               body='{"id": 5001}'),
        _entry("GET", f"{SITE}/api/gone", status=404, body='{"error": "gone"}'),
        _entry("GET", f"{SITE}/api/feed", status=304, mime=""),
        _entry("GET", f"{SITE}/api/feed", body='{"feed": [1]}'),
        _entry("GET", f"{SITE}/api/old", status=302, mime=""),
        _entry("GET", f"{SITE}/api/flaky", status=500, body='{"error": "boom"}'),
        _entry("GET", f"{SITE}/api/flaky", body='{"ok": true}'),
        _entry("POST", f"{SITE}/collect", post=_json_post({"event": "view"}), body="{}"),
        _entry("GET", "https://api.stripe.com/v1/charges", body='{"data": []}'),
    ]


def _capture(domain: str, path: str) -> CaptureResult:
    return CaptureResult(domain=domain, final_url=f"http://{domain}/", requests=[RawRequest(
        url=f"http://{domain}{path}", method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body='{"ok": true}')])


@contextmanager
def _serving(capture: CaptureResult) -> Iterator[httpx.Client]:
    server = MockServer(build_routes(capture), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=server.url) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def mock(tmp_path_factory) -> Iterator[httpx.Client]:
    har = _write(tmp_path_factory.mktemp("har"), _app())
    with _serving(load_traffic(har)) as client:
        yield client


@pytest.fixture
def no_serving(monkeypatch) -> None:
    monkeypatch.setattr(MockServer, "serve_forever", lambda self, poll_interval=0.5: None)


def test_exact_request_gets_its_own_recording(mock):
    response = mock.get("/api/users/1001")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-rebrowse-mock"] == "exact"
    assert response.json() == {"id": 1001, "name": "ann"}
    assert mock.get("/api/users/42").json() == {"id": 42, "name": "bob"}


def test_unseen_id_gets_the_nearest_recording(mock, capsys):
    unseen = mock.get("/api/users/999")

    assert unseen.json() == {"id": 1001, "name": "ann"}
    assert unseen.headers["x-rebrowse-mock"] == "nearest"
    assert unseen.headers["x-rebrowse-route"] == "GET /api/users/{users_id}"
    assert "[mock] 200 GET /api/users/999 (nearest GET /api/users/{users_id})" in (
        capsys.readouterr().err)
    single_digit = mock.get("/api/users/5")
    assert single_digit.headers["x-rebrowse-route"] == "GET /api/users/{users_id}"
    me = mock.get("/api/users/me")
    assert me.json() == {"id": 7, "name": "me"}
    assert me.headers["x-rebrowse-route"] == "GET /api/users/me"


def test_query_pairs_and_bodies_pick_the_recording(mock):
    assert mock.get("/api/items", params={"page": 2, "_": 1700000000}).json() == {"page": 2}
    assert mock.get("/api/items").json() == {"page": 1}
    assert mock.get("/api/items?size=20&page=2").headers["x-rebrowse-mock"] == "exact"
    dogs = mock.post("/api/search", json={"q": "dogs"})
    assert (dogs.json(), dogs.headers["x-rebrowse-mock"]) == ({"hits": 2}, "exact")
    assert mock.post("/api/search", json={"q": "owls"}).json() == {"hits": 1}


def test_graphql_operations_match_by_name(mock):
    feed = mock.post("/graphql", json={
        "operationName": "Feed", "query": "query Feed { feed { id } }", "variables": {"n": 5}})
    user = mock.post("/graphql", json={
        "operationName": "GetUser", "query": "query GetUser { getuser { id } }"})
    unknown = mock.post("/graphql", json={
        "operationName": "DeleteAll", "query": "mutation DeleteAll { deleteAll }"})
    persisted = mock.get("/graphql", params={
        "operationName": "Viewer", "variables": "{}", "extensions": PERSISTED})

    assert feed.json() == {"data": {"feed": []}}
    assert feed.headers["x-rebrowse-mock"] == "nearest"
    assert user.json() == {"data": {"user": {"id": 1}}}
    assert user.headers["x-rebrowse-mock"] == "exact"
    assert user.headers["x-rebrowse-route"] == "POST /graphql [GraphQL query: GetUser]"
    assert (unknown.status_code, unknown.headers["x-rebrowse-mock"]) == (404, "miss")
    assert "data" not in unknown.json()
    assert persisted.json() == {"data": {"viewer": {"id": 7}}}


def test_secrets_and_cookies_are_never_served(tmp_path):
    login = {
        "access_token": "AT_SECRET", "refresh_token": "RT_SECRET", "expires_in": 3600,
        "user": {"name": "ann", "profile": {"password": "PW_SECRET", "city": "Pune"}},
        "attributes": [{"name": "session", "value": "SESSION_SECRET"},
                       {"name": "theme", "value": "dark"}],
    }
    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[RawRequest(
        url=f"{SITE}/api/login", method="POST",
        request_headers={"cookie": "sid=REQUEST_SECRET", "content-type": "application/json"},
        request_body=json.dumps({"user": "ann", "password": "hunter2"}),
        response_status=200,
        response_headers={"content-type": "application/json", "set-cookie": "sid=SETCOOKIE_SECRET"},
        response_body=json.dumps(login),
    )])
    path = save_capture(capture, tmp_path / "capture.json")

    with _serving(load_traffic(path)) as client:
        response = client.post("/api/login", json={"user": "ann", "password": "hunter2"})

    assert response.status_code == 200
    assert b"SECRET" not in response.content
    assert "set-cookie" not in response.headers
    assert not any("SECRET" in value for _, value in response.headers.multi_items())
    served = response.json()
    assert served["access_token"] == served["refresh_token"] == REDACTED
    assert served["expires_in"] == 3600 and served["user"]["name"] == "ann"
    assert served["user"]["profile"] == {"password": REDACTED, "city": "Pune"}
    assert served["attributes"] == [
        {"name": "session", "value": REDACTED}, {"name": "theme", "value": "dark"}]


def test_status_codes_kept_and_redirects_skipped(mock):
    created = mock.post("/api/notes", json={"text": "hi"})
    gone = mock.get("/api/gone")
    feed = mock.get("/api/feed")
    old = mock.get("/api/old")
    flaky = mock.get("/api/flaky")

    assert (created.status_code, created.json()) == (201, {"id": 5001})
    assert (gone.status_code, gone.headers["x-rebrowse-mock"]) == (404, "exact")
    assert gone.json() == {"error": "gone"}
    assert (feed.status_code, feed.json()) == (200, {"feed": [1]})
    assert (old.status_code, old.headers["x-rebrowse-mock"]) == (404, "miss")
    assert (flaky.status_code, flaky.json()) == (200, {"ok": True})
    assert flaky.headers["x-rebrowse-mock"] == "exact"


def test_misses_list_routes_and_exclude_other_sites_and_telemetry(mock, capsys):
    charges = mock.get("/v1/charges")
    collect = mock.post("/collect", json={"event": "view"})
    index = mock.get("/")
    no_id = mock.get("/api/users/")

    for miss in (charges, collect, index, no_id):
        assert (miss.status_code, miss.headers["x-rebrowse-mock"]) == (404, "miss")
    body = collect.json()
    assert (body["error"], body["request"]) == ("no recorded response", "POST /collect")
    assert "GET /api/users/{users_id}" in body["routes"]
    assert not [r for r in body["routes"] if "/v1/charges" in r or "/collect" in r]
    assert not [r for r in body["routes"] if r.split(" ")[1] == "/"]
    assert "[mock] 404 GET /v1/charges (miss)" in capsys.readouterr().err


def test_writes_never_reach_the_recorded_site(tmp_path, fixture_site, hits):
    har = _write(tmp_path, [
        _entry("GET", f"{fixture_site}/api/items", body='{"items": []}'),
        _entry("POST", f"{fixture_site}/api/notes", status=201, post=_json_post({"text": "hi"}),
               body='{"id": 1}'),
        _entry("DELETE", f"{fixture_site}/api/notes/1234", status=204, mime=""),
    ])

    with _serving(load_traffic(har)) as client:
        items = client.get("/api/items")
        note = client.post("/api/notes", json={"text": "new"})
        deleted = client.delete("/api/notes/5678")

    assert items.json() == {"items": []}
    assert (note.status_code, note.json()) == (201, {"id": 1})
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert "content-length" not in deleted.headers
    assert not hits


def test_cors_answers_loopback_origins_only(mock):
    preflight = {"access-control-request-method": "POST",
                 "access-control-request-headers": "content-type"}

    local = mock.options("/api/notes", headers={"origin": "http://localhost:5173", **preflight})
    evil = mock.options("/api/notes", headers={"origin": "https://evil.example", **preflight})
    foreign_read = mock.get("/api/users/1001", headers={"origin": "https://evil.example"})
    local_read = mock.get("/api/users/1001", headers={"origin": "http://127.0.0.1:3000"})

    assert local.status_code == 204
    assert local.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert local.headers["access-control-allow-credentials"] == "true"
    assert local.headers["access-control-allow-headers"] == "content-type"
    assert "POST" in local.headers["access-control-allow-methods"]
    assert evil.status_code == 403 and "access-control-allow-origin" not in evil.headers
    assert foreign_read.status_code == 200
    assert "access-control-allow-origin" not in foreign_read.headers
    assert local_read.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert local_read.headers["vary"] == "Origin"


def test_non_loopback_host_header_is_rejected(mock):
    port = mock.base_url.port

    rebound = mock.get("/api/users/1001", headers={"host": "evil.example"})

    assert rebound.status_code == 403 and "ann" not in rebound.text
    for host in (f"localhost:{port}", f"127.0.0.1:{port}"):
        assert mock.get("/api/users/1001", headers={"host": host}).status_code == 200


def test_responses_are_deterministic(mock):
    first, second = [mock.get("/api/users/999", params={"q": "x"}) for _ in range(2)]

    def headers(response: httpx.Response) -> list:
        return [(k, v) for k, v in response.headers.multi_items() if k != "date"]

    assert first.content == second.content
    assert headers(first) == headers(second)


def test_cli_prints_routes_then_serves(tmp_path, no_serving):
    har = _write(tmp_path, _app(), "e2e.har")

    result = CliRunner().invoke(main, ["mock", str(har), "--port", "0"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"].startswith("http://127.0.0.1:")
    assert (payload["domain"], payload["source"]) == ("app.example.com", str(har.resolve()))
    assert payload["routes"] == [
        {"method": "GET", "path": "/api/feed", "recordings": 1, "statuses": [200]},
        {"method": "GET", "path": "/api/flaky", "recordings": 2, "statuses": [200, 500]},
        {"method": "GET", "path": "/api/gone", "recordings": 1, "statuses": [404]},
        {"method": "GET", "path": "/api/items", "recordings": 3, "statuses": [200]},
        {"method": "POST", "path": "/api/notes", "recordings": 1, "statuses": [201]},
        {"method": "POST", "path": "/api/search", "recordings": 2, "statuses": [200]},
        {"method": "GET", "path": "/api/users/me", "recordings": 1, "statuses": [200]},
        {"method": "GET", "path": "/api/users/{users_id}", "recordings": 2, "statuses": [200]},
        {"method": "GET", "path": "/graphql", "graphql": ["GraphQL operation: Viewer"],
         "recordings": 1, "statuses": [200]},
        {"method": "POST", "path": "/graphql", "graphql": ["GraphQL query: GetUser"],
         "recordings": 1, "statuses": [200]},
        {"method": "POST", "path": "/graphql", "graphql": ["GraphQL query: Feed"],
         "recordings": 1, "statuses": [200]},
    ]


def test_cli_uses_newest_capture_for_a_domain(no_serving):
    captures = config.CAPTURES_DIR
    save_capture(_capture("ex.com", "/api/old"), captures / "ex.com-20261001-000000.json")
    newest = save_capture(_capture("ex.com", "/api/new"), captures / "ex.com-20261002-000000.json")
    save_capture(_capture("ex.com-other", "/api/other"),
                 captures / "ex.com-other-20261003-000000.json")
    save_capture(_capture("127.0.0.1:8080", "/api/local"),
                 captures / "127.0.0.1_8080-20261003-000000.json")
    built = save_capture(_capture("localhost:8080", "/api/built"))
    runner = CliRunner()

    def served(source: str) -> dict:
        result = runner.invoke(main, ["mock", source, "-p", "0"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    by_domain = served("ex.com")
    assert by_domain["source"] == str(newest.resolve())
    assert [row["path"] for row in by_domain["routes"]] == ["/api/new"]
    assert served("https://ex.com/")["source"] == str(newest.resolve())
    assert served("localhost:8080")["source"] == str(built.resolve())
    assert runner.invoke(main, ["mock", "127.0.0.1:80", "-p", "0"]).exit_code == 1


def test_cli_errors_exit_nonzero(tmp_path, no_serving):
    neither = tmp_path / "neither.json"
    neither.write_text('{"not": "a har"}', encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    static = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{SITE}/static/app.css", mime="text/css", body="body {}"),
        _entry("POST", f"{SITE}/collect", post=_json_post({"event": "view"}), body="{}"),
    ], "static.har")
    capture = save_capture(_capture("ex.com", "/api/items"), tmp_path / "ex.json")
    runner = CliRunner()

    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as busy:
        cases = [
            ["nowhere.test"], [str(neither)], [str(broken)], [str(static)],
            [str(capture), "--domain", "other.com"],
            [str(capture), "--port", str(busy.server_address[1])],
        ]
        for args in cases:
            result = runner.invoke(main, ["mock", *args])
            assert result.exit_code == 1, (args, result.output)
            assert "error" in json.loads(result.stdout), args
    assert runner.invoke(main, ["mock", str(capture), "-d", "EX.com", "-p", "0"]).exit_code == 0


def test_group_help_lists_mock():
    assert "mock <source>     serve recorded API responses on localhost" in CliRunner().invoke(
        main, ["--help"]).output


def _raw(method: str, path: str, body: str | None, request_body: str | None = None) -> RawRequest:
    return RawRequest(url=f"{SITE}{path}", method=method, request_body=request_body,
                      response_status=200, response_headers={"content-type": "application/json"},
                      response_body=body)


def _site(*requests: RawRequest) -> CaptureResult:
    return CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=list(requests))


@pytest.mark.parametrize("host", ["[::1]:{port}", "LOCALHOST:{port}", "localhost"])
def test_loopback_host_spellings_are_served(mock, host):
    response = mock.get("/api/users/1001", headers={"host": host.format(port=mock.base_url.port)})

    assert response.status_code == 200


@pytest.mark.parametrize("host", [
    "localhost.evil.example", "127.0.0.1.nip.io:{port}", "2130706433", "evil.example:{port}",
])
def test_lookalike_hosts_are_refused(mock, host):
    response = mock.get("/api/users/1001", headers={"host": host.format(port=mock.base_url.port)})

    assert response.status_code == 403 and "ann" not in response.text


@pytest.mark.parametrize("origin", [
    "http://localhost.evil.example", "http://127.0.0.1.nip.io:5173", "null", "file://",
])
def test_lookalike_origins_get_no_cors(mock, origin):
    preflight = mock.options("/api/notes", headers={
        "origin": origin, "access-control-request-method": "POST"})
    read = mock.get("/api/users/1001", headers={"origin": origin})

    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in read.headers


def test_ipv6_loopback_origin_gets_cors(mock):
    response = mock.get("/api/users/1001", headers={"origin": "http://[::1]:3000"})

    assert response.headers["access-control-allow-origin"] == "http://[::1]:3000"


def test_batched_graphql_is_served_only_to_the_same_batch():
    batch = [{"operationName": "A", "query": "query A { a }"},
             {"operationName": "B", "query": "query B { b }"}]
    answer = [{"data": {"a": 1}}, {"data": {"b": 2}}]
    capture = _site(_raw("POST", "/graphql", json.dumps(answer), json.dumps(batch)))

    with _serving(capture) as client:
        whole = client.post("/graphql", json=batch)
        single = client.post("/graphql", json=batch[0])

    assert (whole.json(), whole.headers["x-rebrowse-mock"]) == (answer, "exact")
    assert whole.headers["x-rebrowse-route"] == (
        "POST /graphql [GraphQL query: A, GraphQL query: B]")
    assert (single.status_code, single.headers["x-rebrowse-mock"]) == (404, "miss")


def test_unparseable_bodies_are_skipped_or_missed():
    deep = "[" * 100_000 + "]" * 100_000
    feed = {"operationName": "Feed", "query": "query Feed { feed }"}
    capture = _site(
        _raw("GET", "/api/deep", deep),
        _raw("POST", "/api/echo", "{}", deep),
        _raw("POST", "/graphql", '{"data": {"feed": []}}', json.dumps(feed)),
    )

    assert [route.name for route in build_routes(capture)] == ["POST /graphql [GraphQL query: Feed]"]
    with _serving(capture) as client:
        for body in (deep.encode(), b"\xff\xfe{", b"{not json"):
            miss = client.post("/graphql", content=body)
            assert (miss.status_code, miss.headers["x-rebrowse-mock"]) == (404, "miss"), body[:8]
        assert client.post("/graphql", json=feed).json() == {"data": {"feed": []}}


def test_a_recording_with_a_body_beats_one_without(capsys):
    capture = _site(_raw("GET", "/api/lists/1001", None),
                    _raw("GET", "/api/lists/1002", '{"rows": [1, 2]}'))

    with _serving(capture) as client:
        dropped = client.get("/api/lists/1001")

    assert (dropped.json(), dropped.headers["x-rebrowse-mock"]) == ({"rows": [1, 2]}, "nearest")
    assert "no body recorded" not in capsys.readouterr().err


def test_a_route_recorded_only_without_a_body_is_flagged(capsys):
    with _serving(_site(_raw("GET", "/api/lists/1001", None))) as client:
        empty = client.get("/api/lists/1001")

    assert (empty.status_code, empty.content) == (200, b"")
    assert ("[mock] 200 GET /api/lists/1001 (exact GET /api/lists/{lists_id}, no body recorded)"
            in capsys.readouterr().err)


@pytest.mark.parametrize("guard", [")]}'\n", ")]}',\n"])
def test_xssi_guarded_json_is_redacted(guard):
    body = guard + json.dumps({"access_token": "XSSI_SECRET", "user": "ann"})

    with _serving(_site(_raw("GET", "/api/session", body))) as client:
        response = client.get("/api/session")

    assert b"SECRET" not in response.content
    assert response.text.startswith(guard)
    assert json.loads(response.text.removeprefix(guard)) == {"access_token": REDACTED, "user": "ann"}


def test_form_fields_pick_the_recording_of_an_rpc_endpoint():
    ajax = "/wp-admin/admin-ajax.php"
    capture = _site(_raw("POST", ajax, '{"posts": [1]}', "action=get_posts&page=1"),
                    _raw("POST", ajax, '{"saved": true}', "action=save_post&id=9"))

    with _serving(capture) as client:
        save = client.post(ajax, data={"action": "save_post", "id": "10"})
        posts = client.post(ajax, data={"action": "get_posts", "page": "3"})
        saved = client.post(ajax, data={"id": "9", "action": "save_post"})

    assert (save.json(), save.headers["x-rebrowse-mock"]) == ({"saved": True}, "nearest")
    assert posts.json() == {"posts": [1]}
    assert (saved.json(), saved.headers["x-rebrowse-mock"]) == ({"saved": True}, "exact")


@pytest.mark.parametrize("content_type", [
    "application/json\r\nset-cookie: injected=1", "application/json; charset=utf‑8",
])
def test_unsafe_recorded_content_types_are_not_sent(tmp_path, content_type):
    har = _write(tmp_path, [_entry("GET", f"{SITE}/api/me", body='{"id": 7}',
                                   response_headers=[("Content-Type", content_type)])])

    with _serving(load_traffic(har)) as client:
        response = client.get("/api/me")

    assert (response.status_code, response.json()) == (200, {"id": 7})
    assert "set-cookie" not in response.headers
    assert "content-type" not in response.headers


def test_ctrl_c_exits_cleanly_and_closes_the_socket(tmp_path, monkeypatch):
    closed: list[MockServer] = []
    close = MockServer.server_close

    def interrupted(self: MockServer, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt

    def recording_close(self: MockServer) -> None:
        closed.append(self)
        close(self)

    monkeypatch.setattr(MockServer, "serve_forever", interrupted)
    monkeypatch.setattr(MockServer, "server_close", recording_close)
    har = _write(tmp_path, _app(), "e2e.har")

    result = CliRunner().invoke(main, ["mock", str(har), "-p", "0"])

    assert result.exit_code == 0, result.output
    assert len(closed) == 1 and closed[0].socket.fileno() == -1


def test_domain_option_mocks_another_site_in_a_har(tmp_path, no_serving):
    har = _write(tmp_path, _app(), "e2e.har")

    result = CliRunner().invoke(main, ["mock", str(har), "-d", "https://api.stripe.com", "-p", "0"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["domain"] == "api.stripe.com"
    assert [row["path"] for row in payload["routes"]] == ["/v1/charges"]


def test_latest_capture_escapes_glob_characters():
    bracketed = save_capture(_capture("[::1]:8080", "/api/six"),
                             config.CAPTURES_DIR / "[__1]_8080-20261001-000000.json")
    save_capture(_capture("1:8080", "/api/four"), config.CAPTURES_DIR / "1_8080-20261002-000000.json")

    assert latest_capture("[::1]:8080") == bracketed
    assert latest_capture("::1") is None


def test_mock_module_imports_no_http_client():
    tree = ast.parse(Path(mock_module.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & HTTP_CLIENTS


def test_graphql_variables_pick_the_recording_within_an_operation():
    def get_user(user_id: int) -> str:
        return json.dumps({"operationName": "GetUser", "query": "query GetUser($id: ID!) { user }",
                           "variables": {"id": user_id}})

    capture = _site(_raw("POST", "/graphql", '{"data": {"user": 1}}', get_user(1)),
                    _raw("POST", "/graphql", '{"data": {"user": 2}}', get_user(2)))

    with _serving(capture) as client:
        second = client.post("/graphql", content=get_user(2))
        unseen = client.post("/graphql", content=get_user(3))

    assert (second.json(), second.headers["x-rebrowse-mock"]) == ({"data": {"user": 2}}, "exact")
    assert (unseen.json(), unseen.headers["x-rebrowse-mock"]) == ({"data": {"user": 1}}, "nearest")


def test_api_on_a_sibling_subdomain_is_served_but_other_sites_are_not():
    def at(host: str, path: str, body: str) -> RawRequest:
        return RawRequest(url=f"https://{host}{path}", method="GET", response_status=200,
                          response_headers={"content-type": "application/json"},
                          response_body=body)

    capture = _site(at("api.example.com", "/v2/orders", '{"orders": []}'),
                    at("api.example.org", "/v2/invoices", '{"invoices": []}'))

    with _serving(capture) as client:
        orders = client.get("/v2/orders")
        invoices = client.get("/v2/invoices")

    assert (orders.status_code, orders.json()) == (200, {"orders": []})
    assert (invoices.status_code, invoices.headers["x-rebrowse-mock"]) == (404, "miss")


def test_an_unrecorded_method_on_a_recorded_path_is_a_miss(mock):
    for method in ("PUT", "PATCH", "DELETE"):
        response = mock.request(method, "/api/notes", json={"text": "hi"})
        assert (response.status_code, response.headers["x-rebrowse-mock"]) == (404, "miss"), method
    assert mock.get("/api/notes").headers["x-rebrowse-mock"] == "miss"


@pytest.mark.parametrize("content_type,body", [
    ("text/plain; charset=utf-8", "id=7&user=ann"),
    ("application/xml", "<user><name>ann</name></user>"),
])
def test_text_bodies_are_served_verbatim_with_their_content_type(content_type, body):
    capture = _site(RawRequest(url=f"{SITE}/api/export", method="GET", response_status=200,
                               response_headers={"content-type": content_type},
                               response_body=body))

    with _serving(capture) as client:
        response = client.get("/api/export")

    assert response.headers["content-type"] == content_type
    assert response.text == body


def test_a_request_without_a_host_header_is_refused(mock):
    with socket.create_connection((mock.base_url.host, mock.base_url.port), timeout=5) as conn:
        conn.sendall(b"GET /api/users/1001 HTTP/1.0\r\n\r\n")
        status_line = conn.makefile("rb").readline()

    assert status_line.split()[1] == b"403"


def test_a_preflight_without_an_origin_is_refused(mock):
    response = mock.options("/api/notes", headers={"access-control-request-method": "POST"})

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_a_stalled_client_does_not_block_other_requests(mock):
    body = json.dumps({"text": "hi"}).encode()
    head = (f"POST /api/notes HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()

    with socket.create_connection((mock.base_url.host, mock.base_url.port), timeout=5) as stalled:
        stalled.sendall(head)
        meanwhile = mock.get("/api/users/1001", timeout=5)
        stalled.sendall(body)
        answer = stalled.makefile("rb").read()

    assert meanwhile.json() == {"id": 1001, "name": "ann"}
    assert answer.startswith(b"HTTP/1.0 201") and answer.endswith(b'{"id": 5001}')


@pytest.mark.parametrize("name", [
    "ex.com-20261009-000000.json.bak", "ex.com-latest.json", "ex.com-2026100-000000.json",
    "ex.com-20261009-000000-copy.json", "xex.com-20261009-000000.json",
])
def test_latest_capture_ignores_files_that_are_not_build_captures(name):
    saved = save_capture(_capture("ex.com", "/api/a"), config.CAPTURES_DIR / "ex.com-20261001-000000.json")
    save_capture(_capture("ex.com", "/api/b"), config.CAPTURES_DIR / name)

    assert latest_capture("ex.com") == saved


def _recorded(path: str, *, method: str = "GET", status: int = 200, content_type: str = "",
              body: str | None = None, request: str | None = None) -> RawRequest:
    headers = {"content-type": content_type} if content_type else {}
    return RawRequest(url=path if "://" in path else f"{SITE}{path}", method=method,
                      request_body=request, response_status=status, response_headers=headers,
                      response_body=body)


def test_empty_success_responses_are_replayed_over_recorded_errors():
    ok, bad = {"user": "ann", "attempt": 1}, {"user": "ann", "attempt": 2}
    capture = _site(
        _recorded("/api/login", method="POST", content_type="text/html", body="",
                  request=json.dumps(ok)),
        _recorded("/api/login", method="POST", status=401, content_type="application/json",
                  body='{"error": "bad credentials"}', request=json.dumps(bad)),
        _recorded("/api/items", method="POST", status=201, request='{"name": "a"}'),
        _recorded("/api/items", method="POST", status=422, content_type="application/json",
                  body='{"error": "name required"}', request='{"name": ""}'),
    )

    with _serving(capture) as client:
        login = client.post("/api/login", json=ok)
        saved = client.post("/api/items", json={"name": "a"})

    assert (login.status_code, login.content, login.headers["x-rebrowse-mock"]) == (
        200, b"", "exact")
    assert (saved.status_code, saved.headers["x-rebrowse-mock"]) == (201, "exact")


def test_sibling_hosts_keep_their_own_routes():
    capture = _site(
        _recorded("https://auth.example.com/v1/me", content_type="application/json",
                  body='{"from": "auth"}'),
        _recorded(f"{SITE}/v1/me", content_type="application/json", body='{"from": "app"}'),
    )

    routes = build_routes(capture)
    with _serving(capture) as client:
        me = client.get("/v1/me")

    assert [route.name for route in routes] == ["GET /v1/me", "GET auth.example.com/v1/me"]
    assert route_table(routes)[1]["host"] == "auth.example.com"
    assert (me.json(), me.headers["x-rebrowse-mock"]) == ({"from": "app"}, "nearest")


def test_bodies_are_served_as_utf8_whatever_charset_was_recorded():
    capture = _site(_recorded("/api/names", content_type="application/json; charset=iso-8859-1",
                              body='{"name": "Jos\u00e9"}'))

    with _serving(capture) as client:
        names = client.get("/api/names")

    assert names.headers["content-type"] == "application/json; charset=utf-8"
    assert names.json() == {"name": "Jos\u00e9"}


@pytest.mark.parametrize("content_type,body,served", [
    ("application/x-www-form-urlencoded", "access_token=gho_SECRET&scope=repo",
     "access_token=%3Credacted%3E&scope=repo"),
    ("application/javascript", 'cb({"access_token": "SECRET", "id": 1});',
     'cb({"access_token":"<redacted>","id":1});'),
    ("application/json", '\ufeff{"access_token": "SECRET"}', '\ufeff{"access_token":"<redacted>"}'),
])
def test_form_jsonp_and_bom_bodies_are_redacted(content_type, body, served):
    with _serving(_site(_recorded("/api/token", content_type=content_type, body=body))) as client:
        token = client.get("/api/token")

    assert token.content.decode("utf-8") == served


def test_cross_site_pages_cannot_read_the_mock(mock):
    evil = mock.get("/api/users/1001", headers={
        "sec-fetch-site": "cross-site", "referer": "https://evil.example/"})
    local = mock.get("/api/users/1001", headers={
        "sec-fetch-site": "cross-site", "referer": "http://localhost:5173/"})

    assert evil.status_code == 403
    assert local.json() == {"id": 1001, "name": "ann"}


def test_large_and_chunked_request_bodies_get_their_recording(mock):
    big = mock.post("/api/notes", content=b'{"text": "hi"}' + b" " * 2_000_000,
                    headers={"content-type": "application/json"})
    chunked = mock.post("/api/notes", content=iter([b'{"text":', b' "hi"}']))

    assert big.status_code == 201
    assert (chunked.status_code, chunked.headers["x-rebrowse-mock"]) == (201, "exact")


def test_graphql_get_queries_are_told_apart():
    viewer = "{ viewer { id } }"
    capture = _site(_recorded(f"/graphql?query={quote(viewer)}", content_type="application/json",
                              body='{"data": {"viewer": {"id": 7}}}'))

    with _serving(capture) as client:
        same = client.get("/graphql", params={"query": viewer})
        other = client.get("/graphql", params={"query": "{ deleteEverything }"})

    assert same.json() == {"data": {"viewer": {"id": 7}}}
    assert (other.status_code, other.headers["x-rebrowse-mock"]) == (404, "miss")


def test_a_malformed_request_target_is_a_400(mock):
    with socket.create_connection((mock.base_url.host, mock.base_url.port), timeout=5) as conn:
        conn.sendall(b"GET http://[::1/api HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        assert b" 400 " in conn.makefile("rb").readline()


def test_cli_finds_busy_ports_and_lists_saved_captures(no_serving):
    capture = save_capture(_capture("ex.com", "/api/items"))
    runner = CliRunner()

    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        taken = runner.invoke(main, ["mock", str(capture), "-p", str(busy.getsockname()[1])])
    unknown = runner.invoke(main, ["mock", "example.org"])

    assert taken.exit_code == 1 and "already in use" in json.loads(taken.stdout)["error"]
    assert "(saved: ex.com)" in json.loads(unknown.stdout)["error"]
    assert runner.invoke(main, ["mock", "EX.com", "-p", "0"]).exit_code == 0


def test_a_lone_surrogate_is_served_as_a_replacement_character():
    with _serving(_site(_raw("GET", "/api/names", '{"name": "\ud800"}'))) as client:
        names = client.get("/api/names")

    assert names.json() == {"name": "?"}
