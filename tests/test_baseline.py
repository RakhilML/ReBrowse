from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from click.testing import CliRunner
from test_contract import ITEMS, RESULTS, _page, _running, _scripted
from test_drift import _get, _users
from test_har import _entry, _json_post, _with_cookies, _write
from test_mock import HTTP_CLIENTS, PERSISTED, SITE, _app, _gql, _serving

from rebrowse import baseline, contract
from rebrowse.baseline import make_baseline
from rebrowse.capture.store import latest_capture, load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.mock import build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.safety import FORM, REDACTED

UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
TOKEN = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"
DOCUMENTS = [
    {"operationName": "Login", "query": 'mutation Login { login(email: "SENTINEL@corp.test", '
                                        'password: "SENTINEL-PW", tries: 98765) { ok } }'},
    {"query": 'mutation { login(email: "ann@corp.test", password: "SENTINEL-HUNTER2") { ok } }'},
    {"operationName": "Bulk",
     "query": 'mutation Bulk { bulk(action: "delete", note: "SENTINEL-NOTE") { ok } }'},
    {"operationName": "User", "query": 'query User { user(id: 1001, token: "SENTINEL") { id } }'},
]
ADD_NOTE = {"operationName": "AddNote",
            "query": "mutation AddNote($text: String!) { addNote(text: $text) { id } }",
            "variables": {"text": "SENTINEL", "pinned": True, "ids": [1001, 1002]},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "ab" * 32}}}


def _cli(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(main, list(args))
    return result.exit_code, json.loads(result.stdout)


def _baseline(source: Path | str, out: Path, *args: str) -> dict:
    code, summary = _cli("baseline", str(source), "-o", str(out), *args)
    assert code == 0, summary
    return summary


def _diff(base: Path, head: Path) -> tuple[int, int, list[dict]]:
    code, report = _cli("diff", str(base), str(head))
    return code, report["breaking"], report["changes"]


def _planned(capture: CaptureResult) -> tuple[list[tuple], list[dict]]:
    replays, skipped = contract.plan(capture, "http://127.0.0.1:9")
    sent = sorted((replay.request.method, str(replay.request.url),
                   sorted(replay.request.headers.items()), replay.request.content)
                  for replay in replays)
    return sent, sorted(skipped, key=lambda row: row["route"])


def _only(**fields: Any) -> RawRequest:
    req = RawRequest(**{"method": "GET", "response_status": 200,
                        "response_headers": {"content-type": "application/json"}, **fields})
    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[req])
    [kept] = make_baseline(capture).requests
    return kept


def _served(body: str | None) -> str | None:
    return _only(url=f"{SITE}/api/a", response_body=body).response_body


def _nested(depth: int, leaf: Any) -> Any:
    return leaf if depth == 0 else {"n": _nested(depth - 1, leaf)}


PAIRS = {
    "removed field": (
        _users({"id": 1001, "email": "a@x.test", "avatar": "a.png"}, {"id": 42, "email": "b@x"}),
        _users({"id": 1001}, {"id": 42})),
    "optional field": (
        _users({"id": 1, "email": "x"}, {"id": 2, "email": "y"}),
        _users({"id": 1, "email": "x"}, {"id": 2})),
    "types": (
        _users({"a": 1.5, "b": "a", "c": 4.0, "d": "A1", "e": True},
               {"a": 2.5, "b": "b", "c": 4, "d": "B2", "e": False}),
        _users({"a": 3, "b": "a", "c": 4, "d": 7, "e": True},
               {"a": 1.5, "b": None, "c": 4.0, "d": 8, "e": 1})),
    "nested and items": (
        [_get("/api/orders/1001", {"items": [{"sku": "a", "qty": 1}], "meta": {"content-type": "x"},
                                   "user": {"name": "ann", "age": 3}}),
         _get("/api/carts/1001", {"lines": [{"sku": "a"}]}),
         _get("/api/cart", {"items": [{"sku": "a"}, {"sku": "b"}]})],
        [_get("/api/orders/1001", {"items": [{"qty": 1}], "meta": {}, "user": None,
                                   "profile": {"bio": "hi", "links": [{"url": "x"}]}}),
         _get("/api/carts/1001", {"lines": []}),
         _get("/api/cart", {"items": [{"sku": "a"}, {"qty": 1}]})]),
    "depth": (
        [_get("/api/shallow", _nested(3, {"x": 1})), _get("/api/deep", _nested(20, {"x": 1})),
         _get("/api/edge", _nested(11, {"x": 1})), _get("/api/past", _nested(12, {"x": 1}))],
        [_get("/api/shallow", _nested(3, {})), _get("/api/deep", _nested(20, {})),
         _get("/api/edge", _nested(11, {"x": "1"})), _get("/api/past", _nested(12, {"x": "1"}))]),
    "map keys": (
        [_get("/api/team", {"members": {"ann@corp.test": {"role": "admin"},
                                        "bob@corp.test": {"role": "dev", "since": 2020}}}),
         _get("/api/state", {"entities": {"users": {"1001": {"name": "a"}, UUID: {"name": "b"}}}}),
         _get("/api/keys", {"k": {TOKEN: 1, "2026-10-06": 2}})],
        [_get("/api/team", {"members": {"eve@corp.test": {"role": 1}}}),
         _get("/api/state", {"entities": {"users": {"42": {}}}}),
         _get("/api/keys", {"k": {TOKEN: "1"}})]),
    "guards and empty bodies": (
        [_entry("GET", f"{SITE}/api/guarded", body=")]}'\n" + json.dumps({"a": 1})),
         _get("/api/list", [{"sku": "a", "a-b": 1, "naïve": True}]),
         _get("/api/save", {"id": 1}), _get("/api/blank", {"ok": True}),
         _entry("GET", f"{SITE}/api/empty", body="")],
        [_entry("GET", f"{SITE}/api/guarded", body=")]}'\n" + json.dumps({"a": "1"})),
         _get("/api/list", [{"sku": "a"}]),
         _entry("GET", f"{SITE}/api/save", status=204, mime=""),
         _entry("GET", f"{SITE}/api/blank", body=""),
         _get("/api/empty", {"ok": True})]),
    "statuses and sign-in pages": (
        [_get("/api/orders/1001", {"total": 5}), _get("/api/gone", {"error": "gone"}, status=404),
         _get("/api/flaky", {"ok": True}, mime="application/json; charset=UTF-8"),
         _get("/api/profile", {"name": "ann"}), _get("/api/lists/1001", {"rows": [1]}),
         _get("/api/moved", {"ok": True}), _get("/api/cached", {"ok": True})],
        [_get("/api/orders/1001", {"error": "boom"}, status=500), _get("/api/gone", {"back": True}),
         _get("/api/flaky", {"ok": True}), _get("/api/flaky", {"error": "missing"}, status=404),
         _entry("GET", f"{SITE}/api/profile", mime="text/html", body="<form>sign in</form>"),
         _entry("GET", f"{SITE}/api/lists/1002"),
         _entry("GET", f"{SITE}/api/moved", status=302, mime=""),
         _entry("GET", f"{SITE}/api/cached", status=304, mime="")]),
    "graphql": (
        [_get("/api/users/1001", {"id": 1001, "name": "ann"}),
         _entry("POST", f"{SITE}/graphql", post=_gql("GetUser"),
                body='{"data": {"user": {"id": 1, "name": "ann"}}}'),
         _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": [1]}}'),
         _entry("POST", f"{SITE}/graphql", post=_json_post(ADD_NOTE),
                body='{"data": {"addNote": {"id": 5001}}}'),
         _get(f"/graphql?operationName=Viewer&extensions={quote(PERSISTED)}",
              {"data": {"viewer": {"id": 7}}}),
         _get("https://api.app.example.com/v1/me", {"id": 1}), _get("/api/legacy", {"ok": True})],
        [_get("/api/users/42", {"id": 42, "name": "bob"}),
         _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": [2]}}'),
         _entry("POST", f"{SITE}/graphql", post=_gql("GetUser"),
                body='{"data": {"user": {"id": 1}}}'),
         _entry("POST", f"{SITE}/graphql", body='{"data": {"addNote": null}}',
                post=_json_post({**ADD_NOTE, "variables": {"text": "x"}})),
         _get(f"/graphql?operationName=Viewer&extensions={quote(PERSISTED)}",
              {"data": {"viewer": {"id": "7"}}}),
         _get("https://api.app.example.com/v1/me", {"id": "1"}), _get("/api/new", {"ok": True})]),
    "special numbers and mixed items": (
        [_get("/api/n", {"a": float("nan"), "b": 1e20, "c": -0.0, "d": float("inf"),
                         "e": [True, 1, 1.0, "x", None, 2.5]})],
        [_get("/api/n", {"a": 1, "b": 1.5, "c": "0", "d": 2, "e": [1, "y"]})]),
    "redirects and other media": (
        [_entry("GET", f"{SITE}/api/session", status=302, mime="",
                response_headers=[("Location", f"{SITE}/login?next=/a")]),
         _entry("GET", f"{SITE}/api/feed", mime="application/json; v=é",
                body=json.dumps({"rows": [1]})),
         _entry("GET", f"{SITE}/api/doc", mime="text/plain", body="plain words")],
        [_get("/api/session", {"user": {"id": 1}}),
         _get("/api/feed", {"rows": ["1"]}),
         _entry("GET", f"{SITE}/api/doc", mime="application/xml", body="<a/>")]),
}


@pytest.mark.parametrize("base,head", PAIRS.values(), ids=PAIRS.keys())
def test_diff_judges_a_baseline_as_it_judges_its_recording(tmp_path, base, head):
    base_har, head_har = _write(tmp_path, base, "base.har"), _write(tmp_path, head, "head.har")
    base_line, head_line = tmp_path / "base.json", tmp_path / "head.json"
    _baseline(base_har, base_line)
    _baseline(head_har, head_line)

    expected = _diff(base_har, head_har)

    assert _diff(base_line, head_har) == expected
    assert _diff(base_har, head_line) == expected
    assert _diff(base_line, head_line) == expected
    assert _diff(base_har, base_line) == (0, 0, [])


def test_no_credential_or_response_value_reaches_the_baseline(tmp_path):
    order = {"email": "SENTINEL-EMAIL", "total": 1234.5, "count": 9876543,
             "owners": {"ann@corp.test": {"name": "SENTINEL-ANN"}, UUID: {"name": "SENTINEL-UUID"}}}
    read = {"operationName": "Me", "query": "query Me($password: String) { me { id } }",
            "variables": {"password": "SENTINEL-PASSWORD"}}
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html>SENTINEL-PAGE</html>"),
        _entry("GET", f"{SITE}/static/app.js", mime="application/javascript",
               body='const key = "SENTINEL-BUNDLE";'),
        _with_cookies(_entry(
            "GET", "https://ann:SENTINEL-USERINFO@app.example.com/api/orders/1001"
                   f";jsessionid=SENTINEL-JSESSION?api_key=SENTINEL-KEY&t={JWT}",
            headers=[("Cookie", "sid=SENTINEL-COOKIE"), ("Authorization", "Bearer SENTINEL-BEARER"),
                     ("X-CSRF-Token", "SENTINEL-CSRF"), ("Accept", "application/json")],
            response_headers=[("Set-Cookie", "sid=SENTINEL-SETCOOKIE")], body=json.dumps(order))),
        _entry("POST", f"{SITE}/graphql", post=_json_post(read), body='{"data": {"me": {}}}'),
        _entry("POST", f"{SITE}/api/notes", status=201, post=_json_post({"text": "SENTINEL-NOTE"}),
               body='{"id": 9876543}'),
        _get("https://api.stripe.com/v1/charges?c=SENTINEL-STRIPE", {"id": "SENTINEL-STRIPE"}),
        _entry("POST", f"{SITE}/collect", post=_json_post({"event": "SENTINEL-TELEMETRY"}),
               body="{}"),
    ])
    out = tmp_path / "api-baseline.json"

    summary = _baseline(har, out)

    data = out.read_bytes()
    for value in ("SENTINEL", "9876543", "1234.5", "ann@corp.test", UUID, JWT, "sid="):
        assert value.encode() not in data, value
    orders, notes, graphql = load_traffic(out).requests
    assert orders.url == f"{SITE}/api/orders/1001?api_key=%3Credacted%3E&t=%3Credacted%3E"
    assert orders.request_headers == {"accept": "application/json"}
    assert orders.response_headers == {"content-type": "application/json"}
    assert json.loads(orders.response_body) == {
        "count": 0, "email": "", "owners": {"0": {"name": ""}}, "total": 0.5}
    assert json.loads(notes.request_body) == {"text": ""}
    assert json.loads(graphql.request_body)["variables"] == {"password": REDACTED}
    assert (summary["recorded"], summary["requests"], summary["routes"]) == (6, 3, 3)


def test_placeholders_keep_each_json_type_in_sorted_order():
    body = {"a": "x", "b": 3, "c": 2.5, "d": 4.0, "e": True, "f": None,
            "g": [{"x": 1}, {"x": 2}, {"y": "z"}], "m": {"1001": {"r": "a"}, "1002": {"r": "b"}}}

    assert _served(json.dumps(body)) == ('{"a":"","b":0,"c":0.5,"d":0,"e":false,"f":null,'
                                         '"g":[{"x":0},{"y":""}],"m":{"0":{"r":""}}}')
    assert _served(json.dumps({"z": [3, 1, 2, 1], "a": [[2], [1]],
                               "m": {"b@x.test": 1, "a@x.test": "x"}})) == (
        '{"a":[[0]],"m":{"0":"","1":0},"z":[0]}')


@pytest.mark.parametrize("body,served", [
    (")]}'\n" + json.dumps({"a": 1}), ')]}\'\n{"a":0}'),
    ("\ufeff" + json.dumps({"a": "x"}), '\ufeff{"a":""}'),
    ("\ufeff)]}'," + json.dumps([1.5]), "\ufeff)]}',[0.5]"),
    ("<html><body>SECRET</body></html>", None),
    ('<?xml version="1.0"?><a>SECRET</a>', None),
    ('cb({"a": "SECRET"})', None),
    ("SECRET", None),
    ("", ""),
    (None, None),
])
def test_bodies_keep_their_guard_and_anything_but_json_is_dropped(body, served):
    assert _served(body) == served


def test_containers_at_the_depth_limit_become_empty():
    body = _nested(11, {"list": [1], "object": {"x": 1}, "text": "SECRET"})

    assert json.loads(_served(json.dumps(body))) == _nested(11, {"list": [], "object": {},
                                                                "text": ""})


def test_a_write_keeps_its_graphql_operation_but_not_its_variables(tmp_path):
    har = _write(tmp_path, [_entry("POST", f"{SITE}/graphql", post=_json_post(ADD_NOTE),
                                   body='{"data": {"addNote": {"id": 5001}}}')])
    out = tmp_path / "api-baseline.json"

    _baseline(har, out)

    [req] = load_traffic(out).requests
    assert json.loads(req.request_body) == {**ADD_NOTE, "variables": {
        "ids": [0], "pinned": False, "text": ""}}
    assert [route.name for route in build_routes(load_traffic(out))] == [
        "POST /graphql [GraphQL mutation: AddNote]"]
    assert _diff(har, out) == (0, 0, [])


def test_a_batch_keeps_its_operations_in_order():
    batch = [{"operationName": "B", "query": "mutation B { b }", "variables": {"v": "SECRET"}},
             {"operationName": "A", "query": "mutation A { a }"}]

    req = _only(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(batch),
                response_body="[]")

    assert json.loads(req.request_body) == [{**batch[0], "variables": {"v": ""}}, batch[1]]


def test_a_read_keeps_its_ids_and_loses_its_secrets():
    query = {"operationName": "User", "query": "query User($id: ID!) { user(id: $id) { id } }",
             "variables": {"id": 1001, "password": "SECRET"}}

    req = _only(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(query),
                request_headers={"Content-Type": "application/json"}, response_body="{}")

    assert json.loads(req.request_body) == {**query,
                                            "variables": {"id": 1001, "password": REDACTED}}


@pytest.mark.parametrize("content_type,body,kept", [
    (FORM, "text=SECRET&draft=1", None),
    ("multipart/form-data; boundary=x", "--x\r\nSECRET\r\n--x--", None),
    ("application/json", json.dumps({"query": "cats", "page": 2}), '{"page":0,"query":""}'),
])
def test_write_bodies_become_placeholders_or_are_dropped(content_type, body, kept):
    req = _only(url=f"{SITE}/api/search", method="POST", request_body=body,
                request_headers={"content-type": content_type}, response_body="{}")

    assert req.request_body == kept


def test_the_same_api_gives_the_same_bytes(tmp_path):
    page = _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")
    note = _entry("POST", f"{SITE}/api/notes", status=201, post=_json_post({"text": "hi"}),
                  body='{"id": 5001}')
    first = [
        page,
        _get("/api/users/1001", {"id": 1001, "name": "ann", "tags": ["a", "b"],
                                 "friends": {"42": {"since": 2020}}}),
        _get("/api/users/42", {"id": 42, "name": "bob", "tags": [], "friends": {}}),
        _get("/api/items?page=1", {"items": [{"id": 1, "sku": "x"}, {"id": 2}], "total": 2}),
        note,
    ]
    second = [
        _get("/api/items?page=1", {"total": 3, "items": [{"id": 9}, {"id": 8, "sku": "q"}]}),
        {**note, "request": {**note["request"], "postData": _json_post({"text": "other"})}},
        _get("/api/users/42", {"friends": {}, "tags": [], "name": "eve", "id": 42}),
        page,
        _get("/api/users/1001", {"id": 1001, "name": "zed", "tags": ["c", "d", "c"],
                                 "friends": {"7": {"since": 1999}, "8": {"since": 2001}}}),
        _get("/api/users/1001", {"id": 1001, "name": "kim", "tags": ["e"],
                                 "friends": {"9": {"since": 2024}}}),
        note,
    ]
    one, two, again = tmp_path / "one.json", tmp_path / "two.json", tmp_path / "again.json"

    _baseline(_write(tmp_path, first, "first.har"), one)
    _baseline(_write(tmp_path, second, "second.har"), two)
    _baseline(one, again)

    data = one.read_bytes()
    assert two.read_bytes() == again.read_bytes() == data
    assert data.endswith(b"}\n") and b"timestamp" not in data
    assert load_traffic(one).final_url == f"{SITE}/"
    assert len(load_traffic(one).requests) == 4


def test_contract_judges_a_baseline_as_it_judges_its_recording(tmp_path, fixture_site, hits):
    site = "http://app.local"
    har = _write(tmp_path, [
        _page(), _get(f"{site}/api/search", {**RESULTS, "total": 3}),
        _get(f"{site}/api/fail", {"ok": True}), _get(f"{site}/moved", ITEMS),
        _get(f"{site}/blocked", {"ok": True}),
        _entry("POST", f"{site}/api/notes", post=_json_post({"text": "hi"}), body='{"id": 1}'),
    ])
    out = tmp_path / "api-baseline.json"
    _baseline(har, out)

    raw_code, raw = _cli("contract", str(har), "--against", fixture_site)
    raw_hits = Counter(hits)
    hits.clear()
    code, report = _cli("contract", str(out), "--against", fixture_site)

    assert code == raw_code == 1
    for key in ("breaking", "changes", "skipped", "replayed", "answered"):
        assert report[key] == raw[key], key
    assert report["skipped"] == [{"route": "POST /api/notes", "reason": "write"}]
    assert hits == raw_hits and hits[("POST", "/api/notes")] == 0
    assert set(hits) == {("GET", "/api/search"), ("GET", "/api/fail"), ("GET", "/moved"),
                         ("GET", "/blocked")}


def test_mock_serves_the_placeholders_with_recorded_statuses_and_types(tmp_path):
    out = tmp_path / "api-baseline.json"
    _baseline(_write(tmp_path, _app()), out)

    with _serving(load_traffic(out)) as client:
        user, gone, charges = (client.get(path) for path in ("/api/users/1001", "/api/gone",
                                                             "/v1/charges"))

    assert (user.status_code, user.headers["content-type"]) == (200, "application/json")
    assert user.content == b'{"id":1001,"name":""}'
    assert (gone.status_code, gone.headers["x-rebrowse-mock"]) == (404, "exact")
    assert (charges.status_code, charges.headers["x-rebrowse-mock"]) == (404, "miss")


def test_a_capture_saved_by_build_keeps_only_what_contract_would_send(tmp_path):
    recorded = RawRequest(
        url=f"{SITE}/api/me", method="GET",
        request_headers={"Cookie": "sid=SENTINEL-COOKIE", "Authorization": "Bearer SENTINEL-BEARER",
                         "X-Api-Key": "SENTINEL-KEY", "User-Agent": "Mozilla/5.0 SENTINEL-UA",
                         "Accept": "application/json", "Content-Type": "application/json"},
        response_status=200,
        response_headers={"content-type": "application/json", "set-cookie": "sid=SENTINEL-SET"},
        response_body=json.dumps({"email": "SENTINEL-EMAIL"}))
    save_capture(CaptureResult(
        domain="app.example.com", final_url=f"{SITE}/home?session=SENTINEL-PAGE",
        requests=[recorded], cookies=[{"name": "sid", "value": "SENTINEL-JAR"}],
        html="<p>SENTINEL-HTML</p>", js_bundles={f"{SITE}/app.js": "SENTINEL-BUNDLE"}))
    out = tmp_path / "api-baseline.json"

    summary = _baseline("app.example.com", out)

    assert summary["source"] == str(latest_capture("app.example.com").resolve())
    assert b"SENTINEL" not in out.read_bytes()
    assert set(json.loads(out.read_bytes())) == {"domain", "final_url", "requests"}
    [req] = load_traffic(out).requests
    sent = contract.replay_request(recorded, "http://127.0.0.1:9").headers
    assert req.request_headers == {"Accept": "application/json", "Content-Type": "application/json"}
    assert {name.lower(): value for name, value in req.request_headers.items()} == {
        name: value for name, value in sent.items() if name not in ("host", "user-agent")}


def test_out_writes_the_file_and_reports_what_it_kept(tmp_path):
    har = _write(tmp_path, _app(), "e2e.har")
    out = tmp_path / "api-baseline.json"

    summary = _baseline(har, out)
    piped = CliRunner().invoke(main, ["baseline", str(har)])

    assert summary == {"source": str(har.resolve()), "domain": "app.example.com",
                       "path": str(out.resolve()), "routes": 12, "recorded": 21, "requests": 17}
    assert len(build_routes(load_traffic(out), redirects=True)) == 12
    assert piped.exit_code == 0 and piped.stdout_bytes == out.read_bytes()
    stdout = tmp_path / "stdout.json"
    stdout.write_bytes(piped.stdout_bytes)
    assert load_traffic(stdout).domain == "app.example.com"


def test_domain_picks_a_sibling_site_in_a_har(tmp_path):
    har = _write(tmp_path, [_entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
                            _get("/api/items", {"items": []}),
                            _get("https://api.app.example.com/v1/me", {"id": 1})])
    out = tmp_path / "api-baseline.json"

    summary = _baseline(har, out, "--domain", "api.app.example.com")

    capture = load_traffic(out)
    assert summary["domain"] == capture.domain == "api.app.example.com"
    assert [route.name for route in build_routes(capture)] == [
        "GET /v1/me", "GET app.example.com/api/items"]


def test_bad_sources_and_outputs_exit_1_and_leave_no_file(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    static = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{SITE}/static/app.css", mime="text/css", body="body {}"),
        _entry("POST", f"{SITE}/collect", post=_json_post({"event": "view"}), body="{}"),
        _get("https://api.stripe.com/v1/charges", {"data": []}),
    ], "static.har")
    har = _write(tmp_path, _app())
    folder = tmp_path / "folder"
    folder.mkdir()
    out = tmp_path / "api-baseline.json"
    cases = [
        (["nowhere.test", "-o", str(out)], "is neither a file nor a domain"),
        ([str(tmp_path / "missing.har"), "-o", str(out)], "is neither a file nor a domain"),
        ([str(broken), "-o", str(out)], f"Cannot read {broken}"),
        ([str(static), "-o", str(out)], f"No API traffic for app.example.com in {static}"),
        ([str(har), "-o", str(folder)], f"Could not write {folder.resolve()}"),
    ]

    for args, error in cases:
        code, report = _cli("baseline", *args)
        assert code == 1 and error in report["error"], (args, report)
    assert not out.exists() and not list(tmp_path.glob(".*"))
    assert not any(folder.iterdir())


def test_lone_surrogates_round_trip(tmp_path):
    query = '{"query": "query Q { q }", "variables": {"s": "\ud800"}}'
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/api/names?q=\ud800", body='{"\\ud800": 1, "n": "\\udc00"}'),
        _entry("POST", f"{SITE}/graphql", post={"mimeType": "application/json", "text": query},
               body="{}"),
    ])
    out, again = tmp_path / "api-baseline.json", tmp_path / "again.json"

    _baseline(har, out)
    _baseline(out, again)

    names, graphql = load_traffic(out).requests
    assert names.url == f"{SITE}/api/names?q=\ud800"
    assert json.loads(names.response_body) == {"0": 0, "n": ""}
    assert graphql.request_body == query
    assert again.read_bytes() == out.read_bytes()


def test_group_help_lists_baseline():
    assert ("baseline <source> write a recording that is safe to commit (no credentials or "
            "response values)" in CliRunner().invoke(main, ["--help"]).output)


def test_baseline_module_imports_no_http_client():
    tree = ast.parse(Path(baseline.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}

    assert not imported & (HTTP_CLIENTS | {"rebrowse.contract"})


@pytest.mark.parametrize("entries", [side for pair in PAIRS.values() for side in pair] + [_app()])
def test_a_baseline_of_a_baseline_is_the_same_file(tmp_path, entries):
    once = tmp_path / "once.json"
    once.write_bytes(baseline.baseline_bytes(make_baseline(load_traffic(_write(tmp_path, entries)))))

    assert baseline.baseline_bytes(make_baseline(load_traffic(once))) == once.read_bytes()


def test_redirect_targets_and_calls_with_a_credential_in_the_path_are_left_out(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{SITE}/api/session", status=302, mime="",
               response_headers=[("Location", f"{SITE}/login?ticket=SENTINEL-LOCATION")]),
        _get(f"/api/reset/{JWT}", {"ok": True}),
        _get("/api/files/Bearer%20SENTINEL-PATH", {"ok": True}),
        _get("/api/items?q=SENTINEL-KEPT&tag=Bearer%20SENTINEL-QUERY", {"items": []}),
        _entry("GET", f"{SITE}/api/odd", mime="application/json; v=\u00e9", body='{"a": 1}'),
    ])
    out = tmp_path / "api-baseline.json"

    summary = _baseline(har, out)

    data = out.read_bytes()
    assert b"SENTINEL-KEPT" in data
    for value in ("SENTINEL-LOCATION", "SENTINEL-PATH", "SENTINEL-QUERY", JWT, "Location"):
        assert value.encode() not in data, value
    items, odd, session = load_traffic(out).requests
    assert (session.response_status, session.response_headers, session.response_body) == (
        302, {}, None)
    assert items.url == f"{SITE}/api/items?q=SENTINEL-KEPT&tag=%3Credacted%3E"
    assert (odd.response_headers, odd.response_body) == ({}, '{"a":0}')
    assert (summary["recorded"], summary["requests"]) == (6, 3)


@pytest.mark.parametrize("entries", [
    [_entry("GET", f"{SITE}/auth/magic/{JWT}", mime="text/html", body="<html></html>"),
     _get("/api/items", {"items": []})],
    [_entry("GET", f"{SITE}/auth/Bearer%20SENTINEL", mime="text/html", body="<html></html>"),
     _get("/api/items", {"items": []})],
    [_get(f"/api/reset/{JWT}", {"ok": True}), _get("/api/items", {"items": []})],
], ids=["magic link", "bearer segment", "first call"])
def test_a_page_url_with_a_credential_in_its_path_becomes_the_site_root(tmp_path, entries):
    out = tmp_path / "api-baseline.json"

    _baseline(_write(tmp_path, entries), out)

    data = out.read_bytes()
    assert load_traffic(out).final_url == f"{SITE}/"
    assert JWT.encode() not in data and b"SENTINEL" not in data


def test_graphql_operations_of_a_saved_capture_keep_no_secret_or_value(tmp_path):
    bodies = [
        {"operationName": "Login", "query": "mutation Login { login { ok } }",
         "extensions": {"recaptchaToken": "SENTINEL-RECAPTCHA"}},
        {"operationName": "Search", "query": {"email": "SENTINEL@corp.test"}},
        {"operationName": {"email": "SENTINEL-NAME"}, "query": "mutation Save { save }",
         "extensions": "SENTINEL-EXTENSIONS"},
    ]
    save_capture(CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(body),
                   request_headers={"Content-Type": "application/json"}, response_status=200,
                   response_headers={"content-type": "application/json"}, response_body="{}")
        for body in bodies]))
    out = tmp_path / "build.json"

    _baseline("app.example.com", out)

    assert b"SENTINEL" not in out.read_bytes()
    assert [json.loads(req.request_body) for req in load_traffic(out).requests] == [
        {"extensions": "", "operationName": {"email": ""}, "query": "mutation Save { save }"},
        {**bodies[0], "extensions": {"recaptchaToken": REDACTED}},
        {"operationName": "Search", "query": {"email": ""}},
    ]
    assert _diff(latest_capture("app.example.com"), out) == (0, 0, [])


def test_a_har_whose_only_call_has_a_credential_in_its_path_has_no_api_traffic(tmp_path):
    har = _write(tmp_path, [_entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
                            _get(f"/api/magic/{JWT}", {"ok": True})])
    out = tmp_path / "api-baseline.json"

    code, report = _cli("baseline", str(har), "-o", str(out))

    assert code == 1 and report["error"] == f"No API traffic for app.example.com in {har}"
    assert not out.exists()


def test_out_in_a_missing_folder_exits_1_and_leaves_nothing(tmp_path):
    har = _write(tmp_path, _app())
    out = tmp_path / "missing" / "api-baseline.json"

    code, report = _cli("baseline", str(har), "-o", str(out))

    assert code == 1 and report["error"].startswith(f"Could not write {out.resolve()}")
    assert not out.parent.exists() and not list(tmp_path.glob(".*"))


def test_request_headers_of_a_saved_capture_are_the_ones_contract_sends(tmp_path):
    recorded = RawRequest(
        url=f"{SITE}/api/me", method="GET", response_status=200,
        request_headers={"X-Custom": "Bearer SENTINEL", "X-Session-Id": "SENTINEL-SID",
                         "X-Request-Signature": "SENTINEL-SIG", "Origin": SITE,
                         "Referer": f"{SITE}/?SENTINEL-REF", "If-None-Match": "W/\"SENTINEL\"",
                         "X-HTTP-Method-Override": "DELETE", "Accept-Encoding": "gzip",
                         "X-Requested-With": "XMLHttpRequest", "Accept-Language": " en-US ",
                         "X-Odd": "caf\u00e9", "Bad Name": "x"},
        response_headers={"content-type": "application/json"}, response_body='{"a": 1}')
    save_capture(CaptureResult(domain="app.example.com", final_url=f"{SITE}/",
                               requests=[recorded]))
    out = tmp_path / "api-baseline.json"

    _baseline("app.example.com", out)

    [req] = load_traffic(out).requests
    sent = contract.replay_request(recorded, "http://127.0.0.1:9").headers
    assert req.request_headers == {"Accept-Language": "en-US",
                                   "X-Requested-With": "XMLHttpRequest"}
    assert {name.lower(): value for name, value in req.request_headers.items()} == {
        name: value for name, value in sent.items() if name not in ("host", "user-agent")}
    assert b"SENTINEL" not in out.read_bytes()


def test_a_form_encoded_graphql_read_keeps_its_query_and_loses_its_secrets():
    body = "query=query+Me+%7B+me+%7B+id+%7D+%7D&id=1001&password=SECRET"

    req = _only(url=f"{SITE}/graphql", method="POST", request_body=body,
                request_headers={"Content-Type": FORM}, response_body="{}")

    assert req.request_body == ("query=query+Me+%7B+me+%7B+id+%7D+%7D&id=1001"
                                "&password=%3Credacted%3E")


def test_a_write_body_past_the_depth_limit_becomes_empty_containers():
    body = {"operationName": "Save", "query": "mutation Save { save }",
            "variables": _nested(20, {"text": "SECRET"})}

    req = _only(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(body),
                response_body="{}")

    assert json.loads(req.request_body) == {**body, "variables": _nested(11, {})}


@pytest.mark.parametrize("order", [1, -1], ids=["as recorded", "reversed"])
def test_contract_judges_a_request_recorded_twice_as_its_recording_does(tmp_path, order):
    site = "http://app.local"
    har = _write(tmp_path, [_page(), *[
        _get(f"{site}/api/notes", {"items": [{"id": 1, "text": "a"}]}),
        _get(f"{site}/api/notes", {"items": []}),
        _get(f"{site}/api/me", {"error": "sign in"}, status=401),
        _get(f"{site}/api/me", {"id": 1}),
    ][::order]])
    out = tmp_path / "api-baseline.json"
    _baseline(har, out)

    with _running(_scripted({
        "/api/notes": (200, "application/json", b'{"items": [{"id": 1}]}'),
        "/api/me": (401, "application/json", b'{"error": "sign in"}'),
    })) as origin:
        raw_code, raw = _cli("contract", str(har), "--against", origin)
        code, report = _cli("contract", str(out), "--against", origin)

    assert code == raw_code == 1
    assert report["changes"] == raw["changes"] == [
        {"severity": "info", "kind": "status", "route": "GET /api/me", "base": [200, 401],
         "head": [401]},
        {"severity": "breaking", "kind": "field_removed", "route": "GET /api/notes",
         "field": "$.items[].text", "base": ["string"], "head": None},
    ]


@pytest.mark.parametrize("saved", [False, True], ids=["har", "saved capture"])
def test_graphql_documents_keep_no_secret_and_writes_keep_no_literal(tmp_path, saved):
    requests = [RawRequest(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(body),
                           request_headers={"Content-Type": "application/json"},
                           response_status=200, response_body='{"data": {}}',
                           response_headers={"content-type": "application/json"})
                for body in DOCUMENTS]
    if saved:
        source = save_capture(CaptureResult(domain="app.example.com", final_url=f"{SITE}/",
                                            requests=requests))
    else:
        source = _write(tmp_path, [_entry("POST", req.url, post=_json_post(body),
                                          headers=[("Content-Type", "application/json")],
                                          body=req.response_body)
                                   for req, body in zip(requests, DOCUMENTS)])
    out = tmp_path / "api-baseline.json"

    _baseline(source, out)

    data = out.read_bytes()
    assert b"SENTINEL" not in data and b"98765" not in data
    assert sorted(json.loads(req.request_body)["query"] for req in load_traffic(out).requests) == [
        'mutation Bulk { bulk(action: "delete", note: "") { ok } }',
        'mutation Login { login(email: "", password: "", tries: 0) { ok } }',
        'mutation { login(email: "ann@corp.test", password: "<redacted>") { ok } }',
        'query User { user(id: 1001, token: "<redacted>") { id } }',
    ]
    assert _diff(source, out) == (0, 0, [])
    sent, skipped = _planned(load_traffic(out))
    assert (sent, skipped) == _planned(load_traffic(source))
    assert [row["reason"] for row in skipped] == ["destructive", "write", "write"]
    assert b"SENTINEL" not in sent[0][3]


def test_tracing_headers_and_cache_busters_leave_the_file_unchanged(tmp_path):
    def run(trace: str, stamp: int) -> list[dict]:
        return [_entry("GET", f"{SITE}/api/items?page=1&_={stamp + n}", body='{"items": []}',
                       headers=[("traceparent", f"00-{trace}{n}-00f067aa0ba902b7-01"),
                                ("sentry-trace", f"{trace}{n}-1"), ("X-Request-Id", f"{trace}{n}"),
                                ("baggage", "sentry-user_id=SENTINEL%40corp.test"),
                                ("Accept", "application/json")])
                for n in range(3)]
    one, two = tmp_path / "one.json", tmp_path / "two.json"

    _baseline(_write(tmp_path, run("4bf92f35", 1759740000000), "one.har"), one)
    _baseline(_write(tmp_path, run("a3ce929d", 1759800000000), "two.har"), two)

    data = one.read_bytes()
    assert two.read_bytes() == data and b"SENTINEL" not in data
    [req] = load_traffic(one).requests
    assert (req.url, req.request_headers) == (f"{SITE}/api/items?page=1",
                                              {"accept": "application/json"})


def _home() -> dict:
    return _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")


def _form_graphql(fields: dict) -> list:
    body = "query=mutation%20AddNote%7BaddNote%7Bid%20title%7D%7D&operationName=AddNote&note=hi"
    return [_home(), _entry("POST", f"{SITE}/graphql", post={"mimeType": FORM, "text": body},
                            body=json.dumps({"data": {"addNote": fields}}))]


def test_a_form_encoded_graphql_write_keeps_its_route(tmp_path):
    base = _write(tmp_path, _form_graphql({"id": 1, "title": "x"}), "base.har")
    head = _write(tmp_path, _form_graphql({"id": 1}), "head.har")
    out = tmp_path / "api-baseline.json"
    _baseline(base, out)

    [kept] = load_traffic(out).requests

    assert "note=" not in kept.request_body and "operationName=AddNote" in kept.request_body
    assert _diff(base, head)[:2] == _diff(out, head)[:2] == (1, 1)


def test_baseline_and_contract_send_the_same_scrubbed_url():
    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/api/plans?tier={quote('Basic plan')}&t={JWT}", method="GET",
                   response_status=200, response_headers={"content-type": "application/json"},
                   response_body="{}")])

    [kept] = make_baseline(capture).requests
    [(_, sent, _, _)] = _planned(capture)[0]

    assert "tier=Basic+plan" in kept.url and JWT not in kept.url
    assert sent.split("/api/plans")[1] == kept.url.split("/api/plans")[1]


def test_query_values_of_writes_are_emptied_but_not_what_classifies_them():
    mutation = "mutation AddNote { addNote(text: \"SENTINEL\") { id } }"
    kept = _only(url=f"{SITE}/api/notes?text=SENTINEL&action=save", method="POST",
                 request_body="{}")
    gql = _only(url=f"{SITE}/graphql?operationName=AddNote&query={quote(mutation)}"
                    f"&variables={quote(json.dumps({'text': 'SENTINEL'}))}")

    assert "SENTINEL" not in kept.url and "action=save" in kept.url
    assert "SENTINEL" not in gql.url and "operationName=AddNote" in gql.url
    assert "mutation" in gql.url


def test_a_token_in_a_matrix_parameter_is_never_kept_or_sent():
    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/api/x;t={JWT}", method="GET", response_status=200,
                   response_headers={"content-type": "application/json"}, response_body="{}")])

    assert make_baseline(capture).requests == []
    assert _planned(capture)[0] == []


def test_a_retried_server_error_does_not_excuse_a_live_one(tmp_path):
    har = _write(tmp_path, [_home(), _get("/api/items", ITEMS, status=500),
                            _get("/api/items", ITEMS)])
    with _running(_scripted({"/api/items": (500, "application/json", b'{"error": "x"}')})) as url:
        code, report = _cli("contract", str(har), "--against", url)

    assert code == 1
    assert [(c["kind"], c["severity"]) for c in report["changes"]] == [("status", "breaking")]
