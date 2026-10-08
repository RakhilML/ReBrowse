from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from test_contract import SITE, _get, _page, _run, _running
from test_har import _entry, _json_post, _write

from rebrowse import contract, follow
from rebrowse.baseline import baseline_bytes
from rebrowse.capture.har import load_har
from rebrowse.capture.store import load_traffic
from rebrowse.cli import main
from rebrowse.models import RawRequest

ITEMS = {"items": [{"id": 1001, "name": "a"}, {"id": 1002, "name": "b"}]}
LIVE_ITEMS = {"items": [{"id": 8810001, "name": "x"}, {"id": 8810002, "name": "y"}]}
DETAIL = "GET /api/items/{items_id}"
UUID_A = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
UUID_B = "0b8e1f4c-2d3a-4e5f-8a9b-7c6d5e4f3a2b"
ORDERS = {"operationName": "Orders", "query": "query Orders { orders { id } }"}
ORDER = {"operationName": "Order", "variables": {"id": UUID_A},
         "query": "query Order($id: ID!) { order(id: $id) { id total } }"}
NO_LINK_NOTE = ("[contract] --follow-ids found no recorded id in another read's answer; if "
                "SOURCE is a baseline written by an older rebrowse, write it again with "
                "'rebrowse baseline'")
GUARD = ")]}'\n"

Answer = Callable[[str, str, bytes], tuple[int, Any]]


class _Origin(BaseHTTPRequestHandler):
    guard = ""

    def log_message(self, *args):
        pass

    def _reply(self, body: bytes) -> None:
        self.server.seen.append((self.command, self.path, body))
        status, payload = self.server.answer(self.command, self.path, body)
        data = (self.guard + json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(b"")

    def do_POST(self):
        self._reply(self.rfile.read(int(self.headers.get("content-length") or 0)))


def _paths(answers: dict[str, Any]) -> Answer:
    """Answer each path with its payload, or (status, payload); anything else is a 404."""
    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        found = answers.get(path, (404, {"error": "not found"}))
        return found if isinstance(found, tuple) else (200, found)
    return answer


def _graphql(method: str, path: str, body: bytes) -> tuple[int, Any]:
    sent = json.loads(body)
    if sent["operationName"] == "Orders":
        return 200, {"data": {"orders": [{"id": UUID_B}]}}
    found = sent["variables"]["id"] == UUID_B
    return 200, {"data": {"order": {"id": UUID_B, "total": 7} if found else None}}


def _contract(source: Path, answer: Answer, *flags: str) -> tuple[Result, dict, list[tuple]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    server.answer, server.seen = answer, []
    with _running(server) as origin:
        result = _run(str(source), "--against", origin, *flags)
    return result, json.loads(result.stdout), server.seen


def _sent(seen: list[tuple]) -> list[str]:
    return [path for _, path, _ in seen]


def _items(tmp_path: Path, *extra: dict) -> Path:
    return _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                             _get("/api/items/1002", {"id": 1002, "name": "b"}), *extra])


def _baseline(source: Path, out: Path) -> Path:
    result = CliRunner().invoke(main, ["baseline", str(source), "-o", str(out)])
    assert result.exit_code == 0, result.output
    return out


def _links(report: dict) -> list[tuple]:
    return [(row["route"], row["in"], row["name"], row["from"]) for row in report["followed"]]


@pytest.mark.parametrize("name, linked", [
    ("id", True), ("ID", True), ("ids", True), ("uuid", True), ("GUID", True),
    ("userId", True), ("orderID", True), ("relatedIds", True), ("orderIDs", True),
    ("user_id", True), ("USER_IDS", True), ("_id", True), ("post-id", True), ("v2Id", True),
    ("paid", False), ("valid", False), ("grid", False), ("android", False), ("idea", False),
    ("sessionId", False), ("tokenId", False), ("csrfToken", False), ("page", False),
])
def test_id_like_names(name, linked):
    assert follow.id_name(name) is linked


def test_positions_are_path_ids_id_named_query_values_and_id_named_body_fields():
    body = [{"operationName": "A", "variables": {"id": 7, "ids": ["u1", 1002], "flag": True,
                                                 "userId": "<redacted>", "pageId": ""}},
            {"variables": {"input": {"teamId": "t-1"}}}]
    req = RawRequest(url=f"{SITE}/api/teams/1001/users/2002?userId=42&page=2&orgId=",
                     method="POST", request_body=json.dumps(body))

    assert [(p.where, p.name, p.value) for p in follow.positions(req)] == [
        ("path", "teams_id", "1001"), ("path", "users_id", "2002"), ("query", "userId", "42"),
        ("body", "$[0].variables.id", 7), ("body", "$[0].variables.ids[0]", "u1"),
        ("body", "$[0].variables.ids[1]", 1002), ("body", "$[1].variables.input.teamId", "t-1"),
    ]
    assert [p.where for p in follow.positions(req.model_copy(update={"method": "GET"}))] == [
        "path", "path", "query"]


def test_a_path_id_is_taken_from_the_targets_own_answer(tmp_path):
    result, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": LIVE_ITEMS, "/api/items/8810002": {"id": 8810002, "name": "y"}}),
        "--follow-ids")

    assert (result.exit_code, report["breaking"], report["changes"]) == (0, 0, [])
    assert report["followed"] == [{"route": DETAIL, "in": "path", "name": "items_id",
                                   "from": "GET /api/items", "field": "$.items[1].id"}]
    assert _sent(seen) == ["/api/items", "/api/items/8810002"]
    assert (report["routes"], report["replayed"], report["answered"]) == (2, 2, 2)
    assert "8810002" not in result.stdout + result.stderr


def test_without_the_flag_requests_are_sent_as_recorded_with_a_hint(tmp_path):
    result, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": LIVE_ITEMS, "/api/items/8810002": {"id": 8810002, "name": "y"}}))

    assert result.exit_code == 1 and "followed" not in report
    assert [(c["kind"], c["route"], c["base"], c["head"]) for c in report["changes"]] == [
        ("status", DETAIL, [200], [404])]
    assert _sent(seen) == ["/api/items", "/api/items/1002"]
    assert result.stderr.strip() == (
        "[contract] 1 route answered 404 or 410 for ids taken from recorded answers; "
        "--follow-ids takes them from ORIGIN's own answers")


def test_a_graphql_variable_is_taken_from_another_operations_answer(tmp_path):
    har = _write(tmp_path, [
        _page(),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ORDERS),
               body=json.dumps({"data": {"orders": [{"id": UUID_A}]}})),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ORDER),
               body=json.dumps({"data": {"order": {"id": UUID_A, "total": 5}}})),
    ])

    result, report, seen = _contract(har, _graphql, "--follow-ids")

    order = json.loads(seen[1][2])
    assert (result.exit_code, report["breaking"]) == (0, 0)
    assert order == {**ORDER, "variables": {"id": UUID_B}}
    assert report["followed"] == [{
        "route": "POST /graphql [GraphQL query: Order]", "in": "body", "name": "$.variables.id",
        "from": "POST /graphql [GraphQL query: Orders]", "field": "$.data.orders[0].id"}]


def test_a_query_id_is_rewritten_and_other_parameters_are_kept(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?userId=1001&page=2", {"invoices": []})])

    result, report, seen = _contract(har, _paths({
        "/api/me": {"id": 3003}, "/api/invoices?userId=3003&page=2": {"invoices": []}}),
        "--follow-ids")

    assert result.exit_code == 0 and report["changes"] == []
    assert _sent(seen) == ["/api/me", "/api/invoices?userId=3003&page=2"]
    assert _links(report) == [("GET /api/invoices", "query", "userId", "GET /api/me")]


def test_a_chain_is_sent_in_dependency_order_from_a_har_and_its_baseline(tmp_path):
    har = _write(tmp_path, [
        _page(), _get("/api/me", {"id": 1001}),
        _get("/api/orders?userId=1001", {"orders": [{"id": 5001, "userId": 1001},
                                                    {"id": 5002, "userId": 1001}]}),
        _get("/api/orders/5002", {"id": 5002, "userId": 1001, "total": 3}),
    ])
    out = _baseline(har, tmp_path / "api-baseline.json")
    target = _paths({"/api/me": {"id": 3003},
                     "/api/orders?userId=3003": {"orders": [{"id": 6001, "userId": 3003},
                                                            {"id": 6002, "userId": 3003}]},
                     "/api/orders/6002": {"id": 6002, "userId": 3003, "total": 4}})

    runs = [_contract(source, target, "--follow-ids") for source in (har, out)]

    recorded = [req.url for req in load_traffic(out).requests]
    assert recorded.index(f"{SITE}/api/orders/5002") < recorded.index(
        f"{SITE}/api/orders?userId=1001")
    for result, report, seen in runs:
        assert (result.exit_code, report["changes"]) == (0, [])
        assert _sent(seen) == ["/api/me", "/api/orders?userId=3003", "/api/orders/6002"]
        assert _links(report) == [
            ("GET /api/orders", "query", "userId", "GET /api/me"),
            ("GET /api/orders/{orders_id}", "path", "orders_id", "GET /api/orders")]


def test_a_read_whose_id_the_target_lacks_is_skipped_not_guessed(tmp_path):
    result, report, seen = _contract(_items(tmp_path), _paths({"/api/items": {"items": []}}),
                                     "--follow-ids")

    assert (result.exit_code, report["changes"]) == (0, [])
    assert _sent(seen) == ["/api/items"]
    assert report["skipped"] == [{"route": DETAIL, "reason": "id not found on target"}]
    assert (report["routes"], report["replayed"], report["answered"]) == (1, 1, 1)


def test_a_failed_source_skips_its_dependent_and_is_reported(tmp_path):
    result, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": (500, {"error": "boom"}), "/api/items/1002": {"id": 1002, "name": "b"}}),
        "--follow-ids")

    assert result.exit_code == 1
    assert _sent(seen) == ["/api/items"]
    assert [(c["kind"], c["route"], c["head"]) for c in report["changes"]] == [
        ("status", "GET /api/items", [500])]
    assert report["skipped"] == [{"route": DETAIL, "reason": "id not found on target"}]


def test_a_missing_index_falls_back_to_the_first_item(tmp_path):
    har = _items(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    server.answer, server.seen = _paths({
        "/api/items": {"items": [{"id": 8810009, "name": "z"}]},
        "/api/items/8810009": {"id": 8810009, "name": "z"}}), []

    with _running(server) as origin:
        report = asyncio.run(contract.run_contract(load_har(har), origin, follow_ids=True))

    assert _sent(server.seen) == ["/api/items", "/api/items/8810009"]
    assert report["breaking"] == 0 and report["followed"][0]["field"] == "$.items[1].id"


@pytest.mark.parametrize("value", ["../admin", "delete-all", "admin", -1, True, {"id": 1},
                                   "1" * 300, "x" * 300, None, 1.5, "a b"])
def test_unsafe_live_values_are_never_sent(tmp_path, value):
    _, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": {"items": [{"id": 8810001, "name": "x"}, {"id": value, "name": "y"}]}}),
        "--follow-ids")

    assert _sent(seen) == ["/api/items"]
    assert {"route": DETAIL, "reason": "id not found on target"} in report["skipped"]


@pytest.mark.parametrize("value", ["a" * 201, "line\nbreak", "tab\there"])
def test_unsafe_live_query_values_are_never_sent(tmp_path, value):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?userId=1001", {"invoices": []})])

    _, report, seen = _contract(har, _paths({"/api/me": {"id": value}}), "--follow-ids")

    assert _sent(seen) == ["/api/me"]
    assert report["skipped"] == [{"route": "GET /api/invoices",
                                  "reason": "id not found on target"}]


def test_a_secret_named_field_is_never_a_source(tmp_path):
    token = "abcdef0123456789abcd"
    har = _write(tmp_path, [_page(), _get("/api/session", {"csrfToken": token}),
                            _get(f"/api/things/{token}", {"id": 1})])

    result, report, seen = _contract(har, _paths({
        "/api/session": {"csrfToken": "ffffffffffffffffffff"}, f"/api/things/{token}": {"id": 1}}),
        "--follow-ids")

    assert report["followed"] == [] and report["changes"] == []
    assert _sent(seen) == ["/api/session", f"/api/things/{token}"]
    assert NO_LINK_NOTE in result.stderr


def test_an_id_under_a_key_that_holds_a_value_is_never_a_source(tmp_path):
    har = _write(tmp_path, [_page(),
                            _get("/api/team", {"members": {"ann@corp.test": {"id": 1001}}}),
                            _get("/api/users/1001", {"id": 1001})])

    result, report, _ = _contract(har, _paths({
        "/api/team": {"members": {"bob@corp.test": {"id": 3003}}},
        "/api/users/1001": {"id": 1001}}), "--follow-ids")

    assert report["followed"] == [] and report["changes"] == []
    assert "corp.test" not in result.stdout + result.stderr


def test_a_read_never_links_to_its_own_answer_or_closes_a_cycle(tmp_path):
    har = _write(tmp_path, [_page(),
                            _get("/api/items/1001", {"id": 1001, "relatedIds": [1002]}),
                            _get("/api/items/1002", {"id": 1002, "relatedIds": [1001]})])

    result, report, seen = _contract(har, _paths({
        "/api/items/1001": {"id": 1001, "relatedIds": [1002]},
        "/api/items/1002": {"id": 1002, "relatedIds": [1001]}}), "--follow-ids")

    assert (result.exit_code, report["changes"]) == (0, [])
    assert report["followed"] == [{"route": DETAIL, "in": "path", "name": "items_id",
                                   "from": DETAIL, "field": "$.relatedIds[0]"}]
    assert _sent(seen) == ["/api/items/1002", "/api/items/1001"]


def test_a_colliding_path_id_is_taken_from_the_list_of_what_it_names(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 13}),
                            _get("/api/products", {"products": [{"id": 12}, {"id": 13}]}),
                            _get("/api/products/13", {"id": 13})])

    result, report, seen = _contract(har, _paths({
        "/api/me": {"id": 1}, "/api/products": {"products": [{"id": 501}, {"id": 502}]},
        "/api/products/502": {"id": 502}}), "--follow-ids")

    assert (result.exit_code, report["changes"]) == (0, [])
    assert report["followed"] == [{"route": "GET /api/products/{products_id}", "in": "path",
                                   "name": "products_id", "from": "GET /api/products",
                                   "field": "$.products[1].id"}]
    assert sorted(_sent(seen)) == ["/api/me", "/api/products", "/api/products/502"]


@pytest.mark.parametrize("answers, source", [
    ({"/api/ads": {"ads": [{"productId": 13}]}, "/api/me": {"id": 13}}, "GET /api/me"),
    ({"/api/a/team": {"members": [{"id": 13}]}, "/api/cart": {"user_id": 13}}, "GET /api/cart"),
    ({"/api/a/team": {"members": [{"id": 13}]}, "/api/users": {"users": [{"id": 13}]}},
     "GET /api/users"),
    ({"/api/a/team": {"members": [{"id": 13}]}, "/api/orgs/70/users": {"users": [{"id": 13}]}},
     "GET /api/orgs/{orgs_id}/users"),
])
def test_a_colliding_query_id_is_taken_from_a_field_named_for_it(tmp_path, answers, source):
    har = _write(tmp_path, [_page(), *(_get(path, body) for path, body in answers.items()),
                            _get("/api/invoices?userId=13", {"invoices": []})])
    replays, _ = contract.plan(load_har(har), "http://127.0.0.1")

    links, _ = follow.link([follow.Read(replay.route.name, replay.sent, replay.recordings)
                            for replay in replays])

    assert [(replays[link.dependent].route.name, replays[link.source].route.name)
            for link in links if link.position.where == "query"] == [("GET /api/invoices", source)]


def test_a_write_is_never_sent_or_followed(tmp_path):
    har = _write(tmp_path, [
        _page(),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body='{"id": 1002}'),
        _get("/api/notes/1002", {"id": 1002, "text": "hi"}),
    ])

    result, report, seen = _contract(har, _paths({
        "/api/notes/1002": {"id": 1002, "text": "hi"}}), "--follow-ids")

    assert ("POST", "/api/notes") not in {(method, path) for method, path, _ in seen}
    assert _sent(seen) == ["/api/notes/1002"]
    assert report["skipped"] == [{"route": "POST /api/notes", "reason": "write"}]
    assert report["followed"] == [] and result.exit_code == 0


def test_the_baseline_keeps_exactly_the_ids_reads_send(tmp_path):
    write = _entry("POST", f"{SITE}/api/items", post=_json_post({"name": "c"}),
                   body='{"id": 1002, "name": "c"}')
    har = _items(tmp_path, write)
    out = _baseline(har, tmp_path / "api-baseline.json")
    again = _baseline(out, tmp_path / "again.json")
    target = _paths({"/api/items": LIVE_ITEMS, "/api/items/8810002": {"id": 8810001}})

    answers = {(req.method, req.url): json.loads(req.response_body)
               for req in load_traffic(out).requests}
    raw, line = (_contract(source, target, "--follow-ids")[1] for source in (har, out))

    assert answers == {
        ("GET", f"{SITE}/api/items"): {"items": [{"id": 0, "name": ""},
                                                 {"id": 1002, "name": ""}]},
        ("GET", f"{SITE}/api/items/1002"): {"id": 1002, "name": ""},
        ("POST", f"{SITE}/api/items"): {"id": 0, "name": ""},
    }
    assert again.read_bytes() == out.read_bytes()
    for key in ("breaking", "changes", "skipped"):
        assert line[key] == raw[key], key
    assert _links(line) == _links(raw) == [(DETAIL, "path", "items_id", "GET /api/items")]


def test_an_old_baseline_follows_nothing_and_says_so(tmp_path):
    out = _baseline(_items(tmp_path), tmp_path / "api-baseline.json")
    old = load_traffic(out)
    for req in old.requests:
        req.response_body = req.response_body.replace("1002", "0")
    out.write_bytes(baseline_bytes(old))
    target = _paths({"/api/items": LIVE_ITEMS, "/api/items/1002": (404, {"error": "gone"})})

    followed, plain = (_contract(out, target, *flags) for flags in (["--follow-ids"], []))

    assert followed[1].pop("followed") == []
    for _, report, _ in (followed, plain):
        report.pop("target")
    assert followed[1] == plain[1] and followed[2] == plain[2]
    assert followed[0].stderr.strip() == NO_LINK_NOTE
    assert plain[0].stderr == "" and followed[0].exit_code == plain[0].exit_code == 1


def test_followed_rows_are_deduplicated_and_sorted(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/1002?view=full", {"id": 1002}),
                            _get("/api/items/1002?view=short", {"id": 1002}),
                            _get("/api/items/1001", {"id": 1001})])

    _, report, seen = _contract(har, _paths({"/api/items": ITEMS}), "--follow-ids")

    assert [row["field"] for row in report["followed"]] == ["$.items[0].id", "$.items[1].id"]
    assert _sent(seen)[0] == "/api/items" and len(seen) == 4


class _Guarded(_Origin):
    guard = GUARD


class _Credentials(_Origin):
    def _reply(self, body: bytes) -> None:
        self.server.credentials.append((self.path, self.headers.get("Authorization"),
                                        self.headers.get("User-Agent")))
        super()._reply(body)


def _orders(tmp_path: Path, recorded: str | int) -> Path:
    order = {**ORDER, "variables": {"id": recorded}}
    return _write(tmp_path, [
        _page(),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ORDERS),
               body=json.dumps({"data": {"orders": [{"id": 1001}]}})),
        _entry("POST", f"{SITE}/graphql", post=_json_post(order),
               body=json.dumps({"data": {"order": {"id": 1001, "total": 5}}})),
    ])


def test_a_value_that_reads_as_a_destructive_path_is_never_sent(tmp_path):
    _, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": {"items": [{"id": 8810001, "name": "x"}, {"id": "delete,all,now"}]}}),
        "--follow-ids")

    assert _sent(seen) == ["/api/items"]
    assert {"route": DETAIL, "reason": "id not found on target"} in report["skipped"]


@pytest.mark.parametrize("recorded, live, sent", [
    (1001, 3003, 3003), ("1001", 3003, "3003"), (1001, "3003", "3003"),
])
def test_a_body_id_stays_an_int_only_when_both_values_are(tmp_path, recorded, live, sent):
    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        if json.loads(body)["operationName"] == "Orders":
            return 200, {"data": {"orders": [{"id": live}]}}
        return 200, {"data": {"order": {"id": 1001, "total": 7}}}

    _, report, seen = _contract(_orders(tmp_path, recorded), answer, "--follow-ids")

    assert json.loads(seen[1][2])["variables"] == {"id": sent}
    assert [row["in"] for row in report["followed"]] == ["body"]


def test_answers_behind_an_xssi_guard_are_followed(tmp_path):
    har = _write(tmp_path, [_page(),
                            _entry("GET", f"{SITE}/api/items", body=GUARD + json.dumps(ITEMS)),
                            _get("/api/items/1002", {"id": 1002, "name": "b"})])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Guarded)
    server.seen = []
    server.answer = _paths({"/api/items": LIVE_ITEMS,
                            "/api/items/8810002": {"id": 8810002, "name": "y"}})

    with _running(server) as origin:
        report = asyncio.run(contract.run_contract(load_har(har), origin, follow_ids=True))

    assert _sent(server.seen) == ["/api/items", "/api/items/8810002"]
    assert report["followed"][0]["field"] == "$.items[1].id" and report["breaking"] == 0


def test_a_followed_request_carries_the_given_credentials_and_not_the_recorded_ones(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "Bearer ci-token")
    recorded = (("Authorization", "Bearer STAGING_SECRET"),)
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS, headers=recorded),
                            _get("/api/items/1002", {"id": 1002, "name": "b"},
                                 headers=recorded)])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Credentials)
    server.seen, server.credentials = [], []
    server.answer = _paths({"/api/items": LIVE_ITEMS,
                            "/api/items/8810002": {"id": 8810002, "name": "y"}})

    with _running(server) as origin:
        result = _run(str(har), "--against", origin, "--header-env", "Authorization=CI_TOKEN",
                      "--follow-ids")

    assert result.exit_code == 0
    assert [(path, auth) for path, auth, _ in server.credentials] == [
        ("/api/items", "Bearer ci-token"), ("/api/items/8810002", "Bearer ci-token")]
    assert len({agent for _, _, agent in server.credentials}) == 1
    assert "STAGING_SECRET" not in result.stdout + result.stderr


def test_no_hint_when_the_recording_itself_answered_404(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/1002", {"error": "gone"}, status=404)])

    result, report, _ = _contract(har, _paths({"/api/items": LIVE_ITEMS}))

    assert (result.exit_code, report["changes"], result.stderr) == (0, [], "")


def test_a_source_that_holds_no_id_drops_the_whole_chain_below_it(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/orders?userId=1001", {"orders": [{"id": 5001}]}),
                            _get("/api/orders/5001", {"id": 5001})])

    result, report, seen = _contract(har, _paths({
        "/api/me": {"id": 3003}, "/api/orders?userId=3003": {"orders": []},
        "/api/orders/5001": {"id": 5001}}), "--follow-ids")

    assert result.exit_code == 0 and report["changes"] == []
    assert _sent(seen) == ["/api/me", "/api/orders?userId=3003"]
    assert report["skipped"] == [{"route": "GET /api/orders/{orders_id}",
                                  "reason": "id not found on target"}]
    assert (report["routes"], report["replayed"], report["answered"]) == (2, 2, 2)


def test_a_query_rewrite_keeps_repeated_parameters_in_place(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?tag=a&userId=1001&tag=b&page=1001",
                                 {"invoices": []})])

    _, report, seen = _contract(har, _paths({"/api/me": {"id": 3003}}), "--follow-ids")

    assert _sent(seen) == ["/api/me", "/api/invoices?tag=a&userId=3003&tag=b&page=1001"]
    assert _links(report) == [("GET /api/invoices", "query", "userId", "GET /api/me")]


def test_the_baseline_keeps_no_secret_named_write_only_or_unsent_value(tmp_path):
    har = _write(tmp_path, [
        _page(),
        _get("/api/items?page=2", {"page": 2, "items": [
            {"id": 1002, "sessionId": 1002, "ownerId": 7777, "parentId": 2}]}),
        _get("/api/items/1002", {"id": 1002}),
        _entry("PUT", f"{SITE}/api/owners/7777", post=_json_post({"name": "n"}),
               body='{"id": 7777}'),
    ])

    out = _baseline(har, tmp_path / "api-baseline.json")

    answers = {req.url: json.loads(req.response_body) for req in load_traffic(out).requests}
    assert answers[f"{SITE}/api/items?page=2"] == {"page": 0, "items": [
        {"id": 1002, "sessionId": 0, "ownerId": 0, "parentId": 0}]}
    assert answers[f"{SITE}/api/owners/7777"] == {"id": 0}


def test_the_baseline_keeps_a_linked_value_only_where_an_id_can_be_followed_from(tmp_path):
    def cart(total: int) -> dict:
        return {"count": total, "rating": total, "total": total, "categoryId": 12,
                "byKey": {"12": {"id": 12}}, "lines": [{"id": 12, "qty": total}]}

    files = [_baseline(_write(tmp_path, [
        _page(), _get("/api/categories/12", {"id": 12}), _get("/api/cart", cart(total))],
        f"{total}.har"), tmp_path / f"{total}.json") for total in (12, 9)]

    answers = {req.url: json.loads(req.response_body)
               for req in load_traffic(files[0]).requests}
    assert answers[f"{SITE}/api/cart"] == {
        "count": 0, "rating": 0, "total": 0, "categoryId": 12, "byKey": {"0": {"id": 0}},
        "lines": [{"id": 12, "qty": 0}]}
    assert files[0].read_bytes() == files[1].read_bytes()


def test_a_route_skipped_for_a_missing_id_leaves_its_accepted_entry_unchecked(tmp_path):
    accepted = tmp_path / "accepted.json"
    entry = {"route": DETAIL, "kind": "status"}
    accepted.write_text(json.dumps([entry]), encoding="utf-8")

    result, report, _ = _contract(_items(tmp_path), _paths({"/api/items": {"items": []}}),
                                  "--follow-ids", "--accepted", str(accepted))

    assert result.exit_code == 0
    assert (report["stale"], report["unchecked"]) == ([], [entry])
    assert "stale" not in result.stderr


def test_small_ids_of_a_freshly_seeded_target_are_followed_into_paths(tmp_path):
    _, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": {"items": [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]},
        "/api/items/2": {"id": 2, "name": "y"}}), "--follow-ids")

    assert _sent(seen) == ["/api/items", "/api/items/2"]
    assert report["skipped"] == []
