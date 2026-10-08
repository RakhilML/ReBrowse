from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from click.testing import CliRunner
from test_contract import SITE, _get, _page
from test_follow_ids import (
    DETAIL,
    ITEMS,
    LIVE_ITEMS,
    ORDER,
    ORDERS,
    UUID_A,
    UUID_B,
    _baseline,
    _contract,
    _paths,
    _sent,
)
from test_har import _entry, _json_post, _write

from rebrowse import follow
from rebrowse.auth.vault import store_api_key
from rebrowse.baseline import baseline_bytes
from rebrowse.capture.store import load_traffic
from rebrowse.cli import main


def _items(tmp_path: Path) -> Path:
    return _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                             _get("/api/items/1002", {"id": 1002, "name": "b"})])


def test_a_followed_query_value_stays_inside_its_own_parameter(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?userId=1001&page=2", {"invoices": []})])

    _, report, seen = _contract(har, _paths({"/api/me": {"id": "a&page=9#x/y z"}}),
                                "--follow-ids")

    sent = urlsplit(seen[1][1])
    assert (seen[1][0], sent.path) == ("GET", "/api/invoices")
    assert parse_qs(sent.query) == {"userId": ["a&page=9#x/y z"], "page": ["2"]}
    assert "a&page=9" not in json.dumps(report)


def test_repeated_id_parameters_each_take_the_value_from_their_own_place(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/prices?ids=1001&ids=1002", {"prices": []})])

    _, report, seen = _contract(har, _paths({"/api/items": LIVE_ITEMS}), "--follow-ids")

    assert _sent(seen) == ["/api/items", "/api/prices?ids=8810001&ids=8810002"]
    assert [(row["name"], row["field"]) for row in report["followed"]] == [
        ("ids", "$.items[0].id"), ("ids", "$.items[1].id")]


def test_a_uuid_from_the_target_replaces_a_numeric_path_id(tmp_path):
    _, report, seen = _contract(_items(tmp_path), _paths({
        "/api/items": {"items": [{"id": UUID_A, "name": "x"}, {"id": UUID_B, "name": "y"}]},
        f"/api/items/{UUID_B}": {"id": 1002, "name": "b"}}), "--follow-ids")

    assert _sent(seen) == ["/api/items", f"/api/items/{UUID_B}"]
    assert report["skipped"] == []
    assert {(c["route"], c["kind"]) for c in report["changes"]} == {
        ("GET /api/items", "field_type")}


def test_a_followed_graphql_body_still_sends_secret_variables_redacted(tmp_path):
    order = {**ORDER, "variables": {"id": UUID_A, "sessionToken": "STAGING_SESSION_SECRET"}}
    har = _write(tmp_path, [
        _page(),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ORDERS),
               body=json.dumps({"data": {"orders": [{"id": UUID_A}]}})),
        _entry("POST", f"{SITE}/graphql", post=_json_post(order),
               body=json.dumps({"data": {"order": {"id": UUID_A, "total": 5}}})),
    ])

    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        if json.loads(body)["operationName"] == "Orders":
            return 200, {"data": {"orders": [{"id": UUID_B}]}}
        return 200, {"data": {"order": {"id": UUID_B, "total": 7}}}

    result, report, seen = _contract(har, answer, "--follow-ids")

    variables = json.loads(seen[1][2])["variables"]
    assert variables["id"] == UUID_B and variables["sessionToken"] != "STAGING_SESSION_SECRET"
    assert all(b"STAGING_SESSION_SECRET" not in body for _, _, body in seen)
    assert [row["name"] for row in report["followed"]] == ["$.variables.id"]
    assert "STAGING_SESSION_SECRET" not in result.stdout + result.stderr


def test_no_hint_for_a_404_on_a_read_whose_id_no_answer_held(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/7777", {"id": 7777, "name": "c"})])

    result, report, _ = _contract(har, _paths({"/api/items": LIVE_ITEMS}))

    assert [c["head"] for c in report["changes"]] == [[404]]
    assert result.exit_code == 1 and result.stderr == ""


def test_kept_ids_change_nothing_diff_reads(tmp_path):
    har = _items(tmp_path)
    out = _baseline(har, tmp_path / "api-baseline.json")
    old = load_traffic(out)
    for req in old.requests:
        req.response_body = req.response_body.replace("1002", "0")
    older = tmp_path / "old-baseline.json"
    older.write_bytes(baseline_bytes(old))

    runs = [CliRunner().invoke(main, ["diff", str(base), str(head)])
            for base, head in ((har, out), (older, out), (out, older))]

    for result in runs:
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["changes"] == []


def test_a_detail_route_skipped_for_a_missing_id_is_named_once(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS),
                            _get("/api/items/1001", {"id": 1001}),
                            _get("/api/items/1002", {"id": 1002})])

    _, report, seen = _contract(har, _paths({"/api/items": {"items": []}}), "--follow-ids")

    assert _sent(seen) == ["/api/items"]
    assert report["skipped"] == [{"route": DETAIL, "reason": "id not found on target"}]
    assert (report["routes"], report["replayed"]) == (1, 1)


def test_a_followed_query_carries_the_targets_query_key_and_not_the_recorded_one(tmp_path):
    store_api_key("127.0.0.1", "k1", auth_type="query")
    har = _write(tmp_path, [_page(), _get("/api/me?api_key=RECORDED", {"id": 1001}),
                            _get("/api/invoices?userId=1001&api_key=RECORDED", {"invoices": []})])

    def answer(method: str, path: str, body: bytes) -> tuple[int, Any]:
        return 200, {"id": 3003} if path.startswith("/api/me") else {"invoices": []}

    result, report, seen = _contract(har, answer, "--follow-ids")

    assert [parse_qs(urlsplit(path).query) for path in _sent(seen)] == [
        {"api_key": ["k1"]}, {"userId": ["3003"], "api_key": ["k1"]}]
    assert "RECORDED" not in result.stdout + result.stderr and report["changes"] == []


def test_an_id_two_routes_hold_equally_is_not_followed(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1}),
                            _get("/api/categories", {"categories": [{"id": 1}, {"id": 2}]}),
                            _get("/api/invoices?userId=1", {"invoices": []})])

    _, report, seen = _contract(har, _paths({
        "/api/me": {"id": 77}, "/api/categories": {"categories": [{"id": 5}]},
        "/api/invoices?userId=1": {"invoices": []}}), "--follow-ids")

    assert "/api/invoices?userId=1" in _sent(seen)
    assert all(row["route"] != "GET /api/invoices" for row in report["followed"])


def test_a_token_shaped_live_id_drops_the_read(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?userId=1001", {"invoices": []})])
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"

    result, report, seen = _contract(har, _paths({"/api/me": {"id": jwt}}), "--follow-ids")

    assert _sent(seen) == ["/api/me"]
    assert {"route": "GET /api/invoices", "reason": "id not found on target"} in report["skipped"]
    assert "1 route skipped: id not found on target" in result.stderr


def test_other_query_parameters_keep_their_bytes(tmp_path):
    har = _write(tmp_path, [_page(), _get("/api/me", {"id": 1001}),
                            _get("/api/invoices?userId=1001&fields=id,name&flag", {"x": []})])

    _, _, seen = _contract(har, _paths({"/api/me": {"id": 3003}}), "--follow-ids")

    assert _sent(seen)[1] == "/api/invoices?userId=3003&fields=id,name&flag"


def test_a_plain_run_links_nothing_when_no_route_is_gone(tmp_path, monkeypatch):
    def unexpected(reads):
        raise AssertionError("follow.link ran without --follow-ids")

    monkeypatch.setattr(follow, "link", unexpected)
    result, report, _ = _contract(_items(tmp_path), _paths({
        "/api/items": ITEMS, "/api/items/1002": {"id": 1002, "name": "b"}}))

    assert result.exit_code == 0 and report["changes"] == []
