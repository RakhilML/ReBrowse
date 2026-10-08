from __future__ import annotations

import json
from typing import Any

from test_contract import SITE, _get, _page
from test_follow_ids import (
    DETAIL,
    ITEMS,
    LIVE_ITEMS,
    UUID_A,
    UUID_B,
    _baseline,
    _contract,
    _paths,
    _sent,
)
from test_har import _entry, _json_post, _write

from rebrowse import contract, follow
from rebrowse.capture.har import load_har
from rebrowse.capture.store import load_traffic
from rebrowse.models import RawRequest


def test_a_read_with_two_linked_ids_is_sent_only_when_both_resolve(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 4410}),
                            _get("/api/orders", {"orders": [{"id": 81723}]}),
                            _get("/api/users/4410/orders/81723", {"id": 81723})])

    result, report, seen = _contract(har, _paths({
        "/api/me": {"id": 7}, "/api/orders": {"orders": []},
        "/api/users/7/orders/81723": {"id": 81723}}), "--follow-ids")

    assert sorted(_sent(seen)) == ["/api/me", "/api/orders"]
    assert report["skipped"] == [{"route": "GET /api/users/{users_id}/orders/{orders_id}",
                                  "reason": "id not found on target"}]
    assert {(row["name"], row["from"]) for row in report["followed"]} == {
        ("users_id", "GET /api/me"), ("orders_id", "GET /api/orders")}
    assert result.exit_code == 0


def test_a_route_with_one_resolved_read_is_replayed_and_counts_only_what_was_sent(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/1002", {"id": 1002, "name": "b"}),
                            _get("/api/items/5555", {"id": 5555, "name": "c"}),
                            _get("/api/tags", {"tags": [{"id": 5555}]})])

    result, report, seen = _contract(har, _paths({
        "/api/items": LIVE_ITEMS, "/api/tags": {"tags": []},
        "/api/items/8810002": {"id": 8810002, "name": "y"}}), "--follow-ids")

    assert "/api/items/8810002" in _sent(seen) and "/api/items/5555" not in _sent(seen)
    assert all(row["route"] != DETAIL for row in report["skipped"])
    assert (report["routes"], report["replayed"], report["answered"]) == (3, 3, 3)
    assert result.exit_code == 0


def test_a_source_whose_answer_is_not_an_object_drops_its_dependent(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/1002", {"id": 1002, "name": "b"})])

    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        return 200, "items"

    _, report, seen = _contract(har, answer, "--follow-ids")

    assert _sent(seen) == ["/api/items"]
    assert {"route": DETAIL, "reason": "id not found on target"} in report["skipped"]


def test_the_hint_counts_gone_routes_and_is_silent_with_the_flag(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 4410}),
                            _get("/api/orders?userId=4410", {"orders": [{"id": 81723}]}),
                            _get("/api/orders/81723", {"id": 81723})])
    target = _paths({"/api/me": {"id": 7}, "/api/orders?userId=4410": (410, {"error": "gone"}),
                     "/api/orders?userId=7": (404, {"error": "no"})})

    plain, followed = (_contract(har, target, *flags)[0] for flags in ([], ["--follow-ids"]))

    assert plain.stderr.strip() == (
        "[contract] 2 routes answered 404 or 410 for ids taken from recorded answers; "
        "--follow-ids takes them from ORIGIN's own answers")
    assert followed.stderr == "[contract] 1 route skipped: id not found on target" + chr(10)
    assert followed.exit_code == 1


def test_without_the_flag_requests_go_in_plan_order_even_when_a_source_comes_later(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items/1002", {"id": 1002, "name": "b"}),
                            _get("/api/zz/items", ITEMS)])
    capture = load_har(har)
    replays, _ = contract.plan(capture, "http://127.0.0.1")
    planned = [replay.request.url.path for replay in replays]

    _, plain, seen = _contract(har, _paths({"/api/zz/items": ITEMS}))
    _, followed, ordered = _contract(har, _paths({"/api/zz/items": ITEMS}), "--follow-ids")

    assert _sent(seen) == planned and "followed" not in plain
    assert planned == ["/api/items/1002", "/api/zz/items"]
    assert _sent(ordered) == ["/api/zz/items", "/api/items/1002"]
    assert [row["from"] for row in followed["followed"]] == ["GET /api/zz/items"]


def test_a_rewrite_changes_only_the_followed_batch_variable():
    batch = [{"operationName": "Order", "variables": {"id": UUID_A, "first": 10},
              "query": "query Order($id: ID!) { order(id: $id) { id } }"},
             {"operationName": "Me", "variables": {"id": UUID_A}, "query": "query Me { me { id } }"}]
    req = RawRequest(url=f"{SITE}/graphql", method="POST", request_body=json.dumps(batch))
    first = next(p for p in follow.positions(req) if p.name == "$[0].variables.id")

    rewritten = follow.rewrite(req, {first: UUID_B})

    assert json.loads(rewritten.request_body) == [
        {**batch[0], "variables": {"id": UUID_B, "first": 10}}, batch[1]]
    assert rewritten.url == req.url


def test_every_index_falls_back_to_the_first_item_in_nested_lists(tmp_path):
    groups = {"groups": [{"items": [{"id": 1001}]},
                         {"items": [{"id": 1003}, {"id": 1004}, {"id": 1002}]}]}
    har = _write(tmp_path, [_page(), _get("/api/groups", groups),
                            _get("/api/items/1002", {"id": 1002})])

    _, report, seen = _contract(har, _paths({
        "/api/groups": {"groups": [{"items": [{"id": 8810007}]}, {"items": [{"id": 8810008}]}]},
        "/api/items/8810007": {"id": 8810007}}), "--follow-ids")

    assert report["followed"][0]["field"] == "$.groups[1].items[2].id"
    assert _sent(seen) == ["/api/groups", "/api/items/8810007"]


def test_a_followed_id_that_now_answers_404_is_a_breaking_change(tmp_path):
    result, report, seen = _contract(_write(tmp_path, [
        _page(), _get("/api/items", ITEMS), _get("/api/items/1002", {"id": 1002, "name": "b"})]),
        _paths({"/api/items": LIVE_ITEMS}), "--follow-ids")

    assert _sent(seen) == ["/api/items", "/api/items/8810002"]
    assert [(c["kind"], c["route"], c["head"]) for c in report["changes"]] == [
        ("status", DETAIL, [404])]
    assert result.exit_code == 1 and "8810002" not in result.stdout + result.stderr


def test_a_baseline_keeps_graphql_and_query_ids_and_follows_them_like_its_har(tmp_path):
    order = {"operationName": "Order", "variables": {"id": UUID_A},
             "query": "query Order($id: ID!) { order(id: $id) { id total } }"}
    orders = {"operationName": "Orders", "query": "query Orders { orders { id } }"}
    har = _write(tmp_path, [
        _page(), _get("/api/me", {"id": 4410, "name": "stage", "sessionIds": [4410]}),
        _get("/api/invoices?userId=4410&page=2", {"invoices": [{"no": "INV-1"}]}),
        _entry("POST", f"{SITE}/graphql", post=_json_post(orders),
               body=json.dumps({"data": {"orders": [{"id": UUID_A}, {"id": UUID_B}]}})),
        _entry("POST", f"{SITE}/graphql", post=_json_post(order),
               body=json.dumps({"data": {"order": {"id": UUID_A, "total": 5}}})),
    ])
    out = _baseline(har, tmp_path / "api-baseline.json")
    live = "c56a4180-65aa-42ec-a945-5fd21dec0538"

    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        if method == "GET":
            return _paths({"/api/me": {"id": 7, "name": "ci", "sessionIds": []},
                           "/api/invoices?userId=7&page=2": {"invoices": []}})(method, path, body)
        if json.loads(body)["operationName"] == "Orders":
            return 200, {"data": {"orders": [{"id": live}]}}
        return 200, {"data": {"order": {"id": live, "total": 1}}}

    runs = [_contract(source, answer, "--follow-ids") for source in (har, out)]

    answers = {(req.url, req.request_body): json.loads(req.response_body)
               for req in load_traffic(out).requests}
    assert answers[(f"{SITE}/api/me", None)] == {"id": 4410, "name": "", "sessionIds": [0]}
    assert answers[(f"{SITE}/graphql", json.dumps(orders))] == {
        "data": {"orders": [{"id": ""}, {"id": UUID_A}]}}
    assert _baseline(out, tmp_path / "again.json").read_bytes() == out.read_bytes()
    (_, from_har, har_seen), (_, from_line, line_seen) = runs
    assert sorted(har_seen) == sorted(line_seen)
    assert from_har["followed"] == [
        {"route": "GET /api/invoices", "in": "query", "name": "userId", "from": "GET /api/me",
         "field": "$.id"},
        {"route": "POST /graphql [GraphQL query: Order]", "in": "body", "name": "$.variables.id",
         "from": "POST /graphql [GraphQL query: Orders]", "field": "$.data.orders[0].id"}]
    assert [row["field"] for row in from_line["followed"]] == ["$.id", "$.data.orders[1].id"]
    for _, report, seen in runs:
        assert (report["breaking"], report["changes"]) == (0, [])
        assert ("GET", "/api/invoices?userId=7&page=2", b"") in seen
        assert json.loads(seen[-1][2])["variables"] == {"id": live}
