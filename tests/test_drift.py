from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_har import _entry, _json_post, _write
from test_mock import HTTP_CLIENTS, SITE, _app, _gql

from rebrowse import config, drift
from rebrowse.capture.store import load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.models import CaptureResult, RawRequest


def _get(path: str, payload, **kwargs) -> dict:
    url = path if "://" in path else f"{SITE}{path}"
    return _entry("GET", url, body=json.dumps(payload), **kwargs)


def _users(*bodies: dict) -> list[dict]:
    return [_get(f"/api/users/{1001 + i}", body) for i, body in enumerate(bodies)]


def _invoke(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(main, ["diff", *args])
    return result.exit_code, json.loads(result.stdout)


def _diff(tmp_path: Path, base: list, head: list, *args: str) -> tuple[int, dict]:
    base_har = _write(tmp_path, base, "base.har")
    head_har = _write(tmp_path, head, "head.har")
    return _invoke(str(base_har), str(head_har), *args)


def _rows(report: dict) -> list[tuple]:
    return [(c["severity"], c["kind"], c["route"], c.get("field"), c["base"], c["head"])
            for c in report["changes"]]


def test_identical_recordings_report_nothing(tmp_path):
    code, report = _diff(tmp_path, _app(), _app())

    facts = drift.route_facts(load_traffic(tmp_path / "base.har"))
    assert (code, report["breaking"], report["changes"]) == (0, 0, [])
    assert report["base"] == {"source": str((tmp_path / "base.har").resolve()),
                              "domain": "app.example.com", "routes": len(facts),
                              "bodies": sum(1 for fact in facts.values() if fact.shape.seen)}
    assert report["head"]["routes"] == len(facts) == 12


def test_removed_fields_break_only_when_every_response_had_them(tmp_path):
    base = _users({"id": 1001, "email": "a@x.test", "avatar": "a.png"},
                  {"id": 42, "email": "b@x.test"})
    head = _users({"id": 1001}, {"id": 42})

    code, report = _diff(tmp_path, base, head)

    route = "GET /api/users/{users_id}"
    assert (code, report["breaking"]) == (1, 1)
    assert _rows(report) == [
        ("info", "field_removed", route, "$.avatar", ["string"], None),
        ("breaking", "field_removed", route, "$.email", ["string"], None),
    ]


@pytest.mark.parametrize("before,after,change", [
    (["A1"], [7], (["string"], ["integer"])),
    (["a", "b"], ["a", None], (["string"], ["null", "string"])),
    ([True], [1], (["boolean"], ["integer"])),
    ([{"id": 1}], [[1]], (["object"], ["array"])),
    ([1.5], [3], None),
    ([[1.5, 2]], [[3]], None),
    ([None, "a"], ["a", None], None),
])
def test_new_types_are_breaking(tmp_path, before, after, change):
    code, report = _diff(tmp_path, _users(*({"v": v} for v in before)),
                         _users(*({"v": v} for v in after)))

    if change is None:
        assert (code, report["changes"]) == (0, [])
    else:
        assert code == 1
        assert _rows(report) == [
            ("breaking", "field_type", "GET /api/users/{users_id}", "$.v", *change)]


def test_a_required_field_becoming_optional_is_breaking(tmp_path):
    always = _users({"id": 1, "email": "x"}, {"id": 2, "email": "y"})
    sometimes = _users({"id": 1, "email": "x"}, {"id": 2})

    code, report = _diff(tmp_path, always, sometimes)
    reverse_code, reverse = _diff(tmp_path, sometimes, always)

    assert code == 1
    assert _rows(report) == [("breaking", "field_optional", "GET /api/users/{users_id}",
                              "$.email", ["string"], ["string"])]
    assert (reverse_code, reverse["changes"]) == (0, [])


def test_nested_fields_and_array_items_get_paths(tmp_path):
    base = [
        _get("/api/orders/1001", {"items": [{"sku": "a", "qty": 1}], "meta": {"content-type": "x"},
                                  "user": {"name": "ann", "age": 3}}),
        _get("/api/carts/1001", {"lines": [{"sku": "a"}]}),
    ]
    head = [
        _get("/api/orders/1001", {"items": [{"qty": 1}], "meta": {},
                                  "profile": {"bio": "hi", "links": [{"url": "x"}]}, "user": None}),
        _get("/api/carts/1001", {"lines": []}),
    ]

    code, report = _diff(tmp_path, base, head)

    route = "GET /api/orders/{orders_id}"
    assert code == 1
    assert _rows(report) == [
        ("breaking", "field_removed", route, "$.items[].sku", ["string"], None),
        ("breaking", "field_removed", route, '$.meta["content-type"]', ["string"], None),
        ("info", "field_added", route, "$.profile", None, ["object"]),
        ("breaking", "field_type", route, "$.user", ["object"], ["null"]),
    ]


def test_fields_below_the_depth_limit_are_not_compared(tmp_path):
    def nested(depth: int, leaf: dict) -> dict:
        return leaf if depth == 0 else {"n": nested(depth - 1, leaf)}

    base = [_get("/api/shallow", nested(3, {"x": 1})), _get("/api/deep", nested(20, {"x": 1}))]
    head = [_get("/api/shallow", nested(3, {})), _get("/api/deep", nested(20, {}))]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert [c["field"] for c in report["changes"]] == ["$.n.n.n.x"]


def test_status_and_content_type_changes(tmp_path):
    base = [
        _get("/api/orders/1001", {"total": 5}),
        _get("/api/gone", {"error": "gone"}, status=404),
        _get("/api/flaky", {"ok": True}, mime="application/json; charset=UTF-8"),
        _get("/api/profile", {"name": "ann"}),
        _get("/api/lists/1001", {"rows": [1]}),
    ]
    head = [
        _get("/api/orders/1001", {"error": "boom"}, status=500),
        _get("/api/gone", {"back": True}),
        _get("/api/flaky", {"ok": True}),
        _get("/api/flaky", {"error": "missing"}, status=404),
        _entry("GET", f"{SITE}/api/profile", mime="text/html", body="<form>sign in</form>"),
        _entry("GET", f"{SITE}/api/lists/1002"),
    ]

    code, report = _diff(tmp_path, base, head)

    assert (code, report["breaking"]) == (1, 2)
    assert _rows(report) == [
        ("info", "status", "GET /api/flaky", None, [200], [200, 404]),
        ("info", "status", "GET /api/gone", None, [404], [200]),
        ("breaking", "status", "GET /api/orders/{orders_id}", None, [200], [500]),
        ("breaking", "content_type", "GET /api/profile", None, ["application/json"],
         ["text/html"]),
    ]


def test_routes_match_on_templates_operations_and_hosts(tmp_path):
    base = [
        _get("/api/users/1001", {"id": 1001, "name": "ann"}),
        _entry("POST", f"{SITE}/graphql", post=_gql("GetUser"),
               body='{"data": {"user": {"id": 1, "name": "ann"}}}'),
        _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": [1]}}'),
        _get("https://api.app.example.com/v1/me", {"id": 1}),
        _get("/api/legacy", {"ok": True}),
    ]
    head = [
        _get("/api/users/42", {"id": 42, "name": "bob"}),
        _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": [2]}}'),
        _entry("POST", f"{SITE}/graphql", post=_gql("GetUser"),
               body='{"data": {"user": {"id": 1}}}'),
        _get("https://api.app.example.com/v1/me", {"id": "1"}),
        _get("/api/new", {"ok": True}),
    ]

    code, report = _diff(tmp_path, base, head)
    only_info = _diff(tmp_path, base[-1:], head[-1:])

    assert code == 1
    assert _rows(report) == [
        ("info", "route_missing", "GET /api/legacy", None, [200], None),
        ("info", "route_added", "GET /api/new", None, None, [200]),
        ("breaking", "field_type", "GET api.app.example.com/v1/me", "$.id", ["integer"],
         ["string"]),
        ("breaking", "field_removed", "POST /graphql [GraphQL query: GetUser]",
         "$.data.user.name", ["string"], None),
    ]
    assert only_info[0] == 0 and only_info[1]["breaking"] == 0
    assert [c["kind"] for c in only_info[1]["changes"]] == ["route_missing", "route_added"]


def test_each_side_defaults_to_its_own_site(tmp_path):
    def recording(site: str, user_id) -> list[dict]:
        return [_entry("GET", f"{site}/", mime="text/html", body="<html></html>"),
                _get(f"{site}/api/users/1001", {"id": user_id}),
                _get(f"{site}/api/items", {"items": []})]

    code, report = _diff(tmp_path, recording("https://prod.example.com", 1001),
                         recording("https://staging.example.com", "1001"))

    assert code == 1
    assert (report["base"]["domain"], report["head"]["domain"]) == (
        "prod.example.com", "staging.example.com")
    assert _rows(report) == [("breaking", "field_type", "GET /api/users/{users_id}", "$.id",
                              ["integer"], ["string"])]


def test_the_report_holds_no_recorded_values(tmp_path):
    login = _json_post({"email": "ann@corp.test", "password": "PWSECRET"})
    base = [
        _entry("POST", f"{SITE}/api/login?api_key=QSECRET", post=login, body=json.dumps(
            {"access_token": "TOKSECRET", "email": "ann@corp.test", "phone": "555-0100"})),
        _get("/api/me?api_key=QSECRET", {"email": "ann@corp.test"}),
    ]
    head = [
        _entry("POST", f"{SITE}/api/login?api_key=QSECRET", post=login, body=json.dumps(
            {"token": "TOKSECRET", "email": {"primary": "ann@corp.test"}})),
        _get("/api/me?api_key=QSECRET", {"error": "ann@corp.test"}, status=500),
    ]

    result = CliRunner().invoke(main, ["diff", str(_write(tmp_path, base, "base.har")),
                                       str(_write(tmp_path, head, "head.har"))])

    assert result.exit_code == 1
    assert len(json.loads(result.stdout)["changes"]) == 5
    for value in ("TOKSECRET", "ann@corp.test", "QSECRET", "PWSECRET", "555-0100"):
        assert value not in result.stdout


def test_the_report_is_deterministic_and_ordered(tmp_path):
    base = [
        _get("/api/zeta", {"z": 1}),
        _get("/api/alpha", {"a": 1, "b": {"c": 1}, "ba": 1}),
    ]
    head = [
        _get("/api/alpha", {"b": {}}),
        _entry("GET", f"{SITE}/api/alpha", mime="text/html", body="<html></html>"),
        _get("/api/alpha", {"error": "x"}, status=404),
        _get("/api/zeta", {"z": "1"}),
    ]
    base_har, head_har = _write(tmp_path, base, "base.har"), _write(tmp_path, head, "head.har")
    runner = CliRunner()

    first, second = (runner.invoke(main, ["diff", str(base_har), str(head_har)]) for _ in range(2))

    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert [(c["route"], c["kind"], c.get("field")) for c in report["changes"]] == [
        ("GET /api/alpha", "status", None),
        ("GET /api/alpha", "content_type", None),
        ("GET /api/alpha", "field_removed", "$.a"),
        ("GET /api/alpha", "field_removed", "$.b.c"),
        ("GET /api/alpha", "field_removed", "$.ba"),
        ("GET /api/zeta", "field_type", "$.z"),
    ]
    assert report["changes"] == drift.compare(load_traffic(base_har), load_traffic(head_har))


def _saved(body: dict) -> CaptureResult:
    return CaptureResult(domain="ex.com", final_url="http://ex.com/", requests=[RawRequest(
        url="http://ex.com/api/items", method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body=json.dumps(body))])


def test_sources_can_be_saved_captures_or_a_host(tmp_path):
    captures = config.CAPTURES_DIR
    old = save_capture(_saved({"items": [], "total": 1}), captures / "ex.com-20261001-000000.json")
    newest = save_capture(_saved({"items": []}), captures / "ex.com-20261002-000000.json")
    har = _write(tmp_path, [_entry("GET", "http://ex.com/api/items", body='{"items": [1]}')])

    code, report = _invoke(str(old), "ex.com")
    same = _invoke("https://EX.com/", str(har))

    assert code == 1
    assert (report["base"]["source"], report["head"]["source"]) == (
        str(old.resolve()), str(newest.resolve()))
    assert [(c["kind"], c["field"]) for c in report["changes"]] == [("field_removed", "$.total")]
    assert same[0] == 0 and same[1]["changes"] == []
    assert same[1]["base"]["source"] == str(newest.resolve())


def test_unreadable_inputs_exit_2(tmp_path):
    neither = tmp_path / "neither.json"
    neither.write_text('{"not": "a har"}', encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    har = _write(tmp_path, _app())

    for bad in ("nowhere.test", str(neither), str(broken)):
        for args in ([bad, str(har)], [str(har), bad]):
            code, report = _invoke(*args)
            assert code == 2 and "error" in report, args
    code, report = _invoke(str(har), str(har), "--domain", "other.example.com")
    assert code == 2 and "No requests to other.example.com" in report["error"]
    assert _invoke(str(har), str(har), "-d", "HTTPS://APP.Example.com/")[0] == 0


def test_drift_module_imports_no_http_client():
    tree = ast.parse(Path(drift.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & HTTP_CLIENTS


def test_group_help_lists_diff():
    assert "diff <a> <b>      report API changes between two recordings" in CliRunner().invoke(
        main, ["--help"]).output


def test_guarded_bodies_root_arrays_and_quoted_keys_are_compared(tmp_path):
    base = [
        _entry("GET", f"{SITE}/api/guarded", body=")]}'\n" + json.dumps({"a": 1})),
        _get("/api/list", [{"sku": "a", "a-b": 1, "naïve": True}]),
    ]
    head = [
        _entry("GET", f"{SITE}/api/guarded", body=")]}'\n" + json.dumps({"a": "1"})),
        _get("/api/list", [{"sku": "a"}]),
    ]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert [(c["kind"], c["route"], c["field"]) for c in report["changes"]] == [
        ("field_type", "GET /api/guarded", "$.a"),
        ("field_removed", "GET /api/list", '$[]["a-b"]'),
        ("field_removed", "GET /api/list", '$[]["naïve"]'),
    ]


def test_fields_are_compared_down_to_depth_12(tmp_path):
    def nested(depth: int, leaf: dict) -> dict:
        return leaf if depth == 0 else {"n": nested(depth - 1, leaf)}

    base = [_get("/api/edge", nested(11, {"x": 1})), _get("/api/past", nested(12, {"x": 1}))]
    head = [_get("/api/edge", nested(11, {"x": "1"})), _get("/api/past", nested(12, {"x": "1"}))]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [("breaking", "field_type", "GET /api/edge", "$" + ".n" * 11 + ".x",
                              ["integer"], ["string"])]


def test_a_sometimes_null_object_reports_only_its_type(tmp_path):
    base = _users({"user": {"name": "a"}}, {"user": {"name": "b"}})
    head = _users({"user": {"name": "a"}}, {"user": None})

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [("breaking", "field_type", "GET /api/users/{users_id}", "$.user",
                              ["object"], ["null", "object"])]


def test_media_types_ignore_case_and_parameters_and_text_bodies_have_no_fields(tmp_path):
    base = [
        _get("/api/data", {"a": 1}),
        _entry("GET", f"{SITE}/api/plain", mime="text/plain", body="ok"),
    ]
    head = [
        _get("/api/data", {"a": 1}, mime="Application/JSON; charset=UTF-8"),
        _entry("GET", f"{SITE}/api/plain", mime="text/plain; charset=utf-8", body="changed"),
    ]

    code, report = _diff(tmp_path, base, head)

    assert (code, report["changes"]) == (0, [])


def test_json_turning_into_no_content_or_a_redirect_is_breaking(tmp_path):
    base = [_get("/api/save", {"id": 1}), _get("/api/moved", {"ok": True}),
            _get("/api/cached", {"ok": True})]
    head = [_entry("GET", f"{SITE}/api/save", status=204, mime=""),
            _entry("GET", f"{SITE}/api/moved", status=302, mime=""),
            _entry("GET", f"{SITE}/api/cached", status=304, mime="")]

    code, report = _diff(tmp_path, base, head)

    assert (code, report["breaking"]) == (1, 2)
    assert _rows(report) == [
        ("info", "status", "GET /api/cached", None, [200], [304]),
        ("breaking", "status", "GET /api/moved", None, [200], [302]),
        ("info", "status", "GET /api/save", None, [200], [204]),
        ("breaking", "body_empty", "GET /api/save", None, ["application/json"], []),
    ]


def test_domain_must_match_a_saved_capture(tmp_path):
    capture = save_capture(_saved({"items": []}), tmp_path / "capture.json")

    code, report = _invoke(str(capture), str(capture), "-d", "other.com")

    assert code == 2 and "is a capture of ex.com" in report["error"]
    assert _invoke(str(capture), str(capture), "-d", "EX.com")[0] == 0


def test_keys_that_hold_values_are_compared_as_one_map_entry(tmp_path):
    base = [_get("/api/team", {"members": {"ann@corp.test": {"role": "admin"}}}),
            _get("/api/state", {"entities": {"users": {"1001": {"name": "ann"}}}})]
    same = [_get("/api/team", {"members": {"bob@corp.test": {"role": "admin"}}}),
            _get("/api/state", {"entities": {"users": {"42": {"name": "bob"}}}})]
    changed = [_get("/api/team", {"members": {"bob@corp.test": {"role": 1}}}),
               _get("/api/state", {"entities": {"users": {"42": {}}}})]

    code, report = _diff(tmp_path, base, same)
    result = CliRunner().invoke(main, ["diff", str(tmp_path / "base.har"),
                                       str(_write(tmp_path, changed, "changed.har"))])

    assert (code, report["changes"]) == (0, [])
    assert _rows(json.loads(result.stdout)) == [
        ("breaking", "field_removed", "GET /api/state", "$.entities.users.*.name", ["string"], None),
        ("breaking", "field_type", "GET /api/team", "$.members.*.role", ["string"], ["integer"]),
    ]
    for value in ("ann@corp.test", "bob@corp.test", "1001", '"42"'):
        assert value not in result.stdout


@pytest.mark.parametrize("key,path", [
    ("7", "$.*"),
    ("1001", "$.*"),
    ("ann@corp.test", "$.*"),
    ("2026-10-06", "$.*"),
    ("Ann Smith", "$.*"),
    ("f47ac10b-58cc-4372-a567-0e02b2c3d479", "$.*"),
    ("a5f1d7e3b9a2c4d6e8f0a1b2", "$.*"),
    ("bXz2kQ1mVt9RfL4pW0a8", "$.*"),
    ("v1", "$.v1"),
    ("sha256", "$.sha256"),
    ("address_line_2", "$.address_line_2"),
    ("accessTokenExpiresIn", "$.accessTokenExpiresIn"),
    ("@type", '$["@type"]'),
    ("content-type", '$["content-type"]'),
])
def test_field_names_are_told_apart_from_keys_that_hold_values(key, path):
    changes = drift.compare(_saved({key: 1}), _saved({key: "1"}))

    assert [(c["kind"], c["field"]) for c in changes] == [("field_type", path)]


@pytest.mark.parametrize("name", [
    "billing_address_line_1", "shipping_address_2", "customAttribute1", "oauth2RedirectUri",
    "http2PushEnabled", "Custom_Field_1__c", "utf8EncodedValue", "iso8601Timestamp",
    "sha256Fingerprint", "x509v3Extensions", "addressLine1234",
])
def test_field_names_with_digits_are_checked_as_fields(name):
    base = _saved({"id": 1, name: "a"})

    removed = drift.compare(base, _saved({"id": 1}))
    retyped = drift.compare(base, _saved({"id": 1, name: None}))

    assert [(c["severity"], c["kind"], c["field"]) for c in removed] == [
        ("breaking", "field_removed", f"$.{name}")]
    assert [(c["kind"], c["field"]) for c in retyped] == [("field_type", f"$.{name}")]


def test_removing_numbered_address_fields_fails_the_diff(tmp_path):
    base = [_get("/api/account", {"id": 1, "shipping_address_2": "Apt 4",
                                  "billing_address_line_1": "1 Main St"})]
    head = [_get("/api/account", {"id": 1})]

    code, report = _diff(tmp_path, base, head)

    assert (code, report["breaking"]) == (1, 2)
    assert [c["field"] for c in report["changes"]] == [
        "$.billing_address_line_1", "$.shipping_address_2"]
    assert "Apt 4" not in json.dumps(report) and "Main St" not in json.dumps(report)


@pytest.mark.parametrize("before,after,changes", [
    ({"accessToken": "abc"}, {"accessToken": None},
     [("field_type", "$.accessToken", ["string"], ["null"])]),
    ({"menu": [{"key": "home"}]}, {"menu": [{"key": None}]},
     [("field_type", "$.menu[].key", ["string"], ["null"])]),
    ({"session": {"user": {"id": 1, "name": "a"}, "expires": "x"}}, {"session": {"user": None}},
     [("field_removed", "$.session.expires", ["string"], None),
      ("field_type", "$.session.user", ["object"], ["null"])]),
])
def test_fields_with_secret_names_are_compared_by_type(before, after, changes):
    report = drift.compare(_saved(before), _saved(after))

    assert [(c["kind"], c["field"], c["base"], c["head"]) for c in report] == changes
    assert {c["severity"] for c in report} == {"breaking"}


def test_a_lone_surrogate_in_a_body_is_compared_not_a_crash(tmp_path):
    code, report = _diff(tmp_path, [_get("/api/names", {"n": "a"})],
                         [_entry("GET", f"{SITE}/api/names", body='{"n": "\ud800", "m": 1}')])

    assert code == 0
    assert _rows(report) == [("info", "field_added", "GET /api/names", "$.m", None, ["integer"])]


def test_a_required_field_missing_or_null_in_head_is_both_optional_and_retyped(tmp_path):
    code, report = _diff(tmp_path, _users({"email": "a"}, {"email": "b"}),
                         _users({}, {"email": None}))

    route = "GET /api/users/{users_id}"
    assert code == 1
    assert _rows(report) == [
        ("breaking", "field_optional", route, "$.email", ["string"], ["null"]),
        ("breaking", "field_type", route, "$.email", ["string"], ["null"]),
    ]


def test_array_items_compare_requiredness_across_all_items(tmp_path):
    base = [_get("/api/cart", {"items": [{"sku": "a"}, {"sku": "b"}]})]
    head = [_get("/api/cart", {"items": [{"sku": "a"}, {"qty": 1}]})]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [
        ("info", "field_added", "GET /api/cart", "$.items[].qty", None, ["integer"]),
        ("breaking", "field_optional", "GET /api/cart", "$.items[].sku", ["string"], ["string"]),
    ]


@pytest.mark.parametrize("before,after,changes", [
    ({"a": 1}, [{"a": 1}], [("field_type", "$", ["object"], ["array"])]),
    ({"x": [[{"a": 1}]]}, {"x": [[{}]]}, [("field_removed", "$.x[][].a", ["integer"], None)]),
    ({"x": None}, {"x": "a"}, [("field_type", "$.x", ["null"], ["string"])]),
    ({"_id": 1, "__typename": "U", "$ref": "r"}, {"_id": "1"},
     [("field_removed", '$["$ref"]', ["string"], None),
      ("field_removed", "$.__typename", ["string"], None),
      ("field_type", "$._id", ["integer"], ["string"])]),
])
def test_root_nested_arrays_and_underscore_names(before, after, changes):
    report = drift.compare(_saved(before), _saved(after))

    assert [(c["kind"], c["field"], c["base"], c["head"]) for c in report] == changes


def test_graphql_errors_replacing_data_are_reported_without_messages(tmp_path):
    base = [_entry("POST", f"{SITE}/graphql", post=_gql("GetUser"),
                   body='{"data": {"user": {"id": 1}}}')]
    head = [_entry("POST", f"{SITE}/graphql", post=_gql("GetUser"), body=json.dumps(
        {"data": None, "errors": [{"message": "denied for ann@corp.test"}]}))]

    result = CliRunner().invoke(main, ["diff", str(_write(tmp_path, base, "base.har")),
                                       str(_write(tmp_path, head, "head.har"))])

    route = "POST /graphql [GraphQL query: GetUser]"
    assert result.exit_code == 1
    assert _rows(json.loads(result.stdout)) == [
        ("breaking", "field_type", route, "$.data", ["object"], ["null"]),
        ("info", "field_added", route, "$.errors", None, ["array"]),
    ]
    assert "ann@corp.test" not in result.stdout and "denied" not in result.stdout


def test_a_route_recorded_without_base_bodies_gets_no_field_changes(tmp_path):
    code, report = _diff(tmp_path, [_entry("GET", f"{SITE}/api/items")],
                         [_get("/api/items", {"items": [1]})])

    assert (code, report["changes"]) == (0, [])


def test_status_changes_between_errors_are_info(tmp_path):
    code, report = _diff(tmp_path, [_get("/api/gone", {"e": 1}, status=404)],
                         [_get("/api/gone", {"e": 1}, status=500)])

    assert code == 0
    assert _rows(report) == [("info", "status", "GET /api/gone", None, [404], [500])]


def test_a_new_media_type_is_not_breaking_when_base_sent_none(tmp_path):
    code, report = _diff(tmp_path, [_get("/api/a", {"x": 1}, mime="")],
                         [_entry("GET", f"{SITE}/api/a", mime="text/html", body="<html></html>")])

    assert (code, report["changes"]) == (0, [])


def test_html_next_to_json_is_breaking_and_json_fields_are_still_compared(tmp_path):
    base = [_get("/api/a", {"x": 1, "y": 1})]
    head = [_get("/api/a", {"x": 1}),
            _entry("GET", f"{SITE}/api/a", mime="text/html; charset=utf-8", body="<form></form>")]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [
        ("breaking", "content_type", "GET /api/a", None, ["application/json"],
         ["application/json", "text/html"]),
        ("breaking", "field_removed", "GET /api/a", "$.y", ["integer"], None),
    ]


def test_base64_bodies_are_compared(tmp_path):
    def encoded(payload: dict) -> dict:
        text = base64.b64encode(json.dumps(payload).encode()).decode()
        return _entry("GET", f"{SITE}/api/a", body=text, encoding="base64")

    code, report = _diff(tmp_path, [encoded({"x": 1})], [encoded({"x": "1"})])

    assert code == 1
    assert _rows(report) == [("breaking", "field_type", "GET /api/a", "$.x", ["integer"],
                              ["string"])]


def test_local_sites_on_different_ports_line_up(tmp_path):
    def recording(port: int, total) -> list[dict]:
        site = f"http://localhost:{port}"
        return [_entry("GET", f"{site}/", mime="text/html", body="<html></html>"),
                _get(f"{site}/api/orders/1001", {"total": total})]

    code, report = _diff(tmp_path, recording(8080, "1.00"), recording(9090, None))

    assert code == 1
    assert (report["base"]["domain"], report["head"]["domain"]) == ("localhost:8080",
                                                                    "localhost:9090")
    assert _rows(report) == [("breaking", "field_type", "GET /api/orders/{orders_id}",
                              "$.total", ["string"], ["null"])]


def test_domain_applies_to_both_sides(tmp_path):
    base = [_get("https://prod.example.com/api/a", {"x": 1})]
    head = [_get("https://staging.example.com/api/a", {"x": 1})]

    code, report = _diff(tmp_path, base, head, "-d", "prod.example.com")

    assert code == 2 and "staging.example.com" in report["error"]


def test_a_body_nested_past_the_recursion_limit_is_not_a_crash(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    result = CliRunner().invoke(main, [
        "diff", str(_write(tmp_path, [_get("/api/a", {"x": 1})], "base.har")),
        str(_write(tmp_path, [_entry("GET", f"{SITE}/api/a", body=deep)], "head.har"))])

    report = json.loads(result.stdout)
    assert result.exit_code == 2 and report["error"].startswith("No API traffic")


def test_error_pages_do_not_count_as_a_new_media_type(tmp_path):
    base = [_get("/api/a", {"x": 1})]
    head = [_get("/api/a", {"x": 1}),
            _entry("GET", f"{SITE}/api/a", status=503, mime="text/html", body="<h1>down</h1>")]

    code, report = _diff(tmp_path, base, head)

    assert (code, report["breaking"]) == (1, 1)
    assert _rows(report) == [("breaking", "status", "GET /api/a", None, [200], [200, 503])]


def test_the_same_path_on_a_sibling_host_is_its_own_route(tmp_path):
    def recording(sibling_id) -> list[dict]:
        return [_get("/v1/me", {"id": 1}),
                _get("https://api.app.example.com/v1/me", {"id": sibling_id})]

    code, report = _diff(tmp_path, recording(1), recording("1"))

    assert code == 1
    assert _rows(report) == [("breaking", "field_type", "GET api.app.example.com/v1/me", "$.id",
                              ["integer"], ["string"])]


def test_query_strings_do_not_split_a_route(tmp_path):
    code, report = _diff(tmp_path, [_get("/api/search?q=shoes&page=1", {"hits": [1]})],
                         [_get("/api/search?q=hats", {"hits": [2]})])

    assert (code, report["changes"]) == (0, [])
    assert report["base"]["routes"] == report["head"]["routes"] == 1


def test_a_required_field_of_an_optional_object_is_still_required(tmp_path):
    base = _users({"profile": {"bio": "x"}}, {})
    head = _users({"profile": {}})

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [("breaking", "field_removed", "GET /api/users/{users_id}",
                              "$.profile.bio", ["string"], None)]


def test_a_head_without_api_calls_is_an_input_error(tmp_path):
    code, report = _diff(tmp_path, _app(), [_entry("GET", f"{SITE}/", mime="text/html",
                                                   body="<html></html>")])

    assert code == 2 and report["error"].startswith("No API traffic for app.example.com")


def _run(*args: str, seed: str, data: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "REBROWSE_DATA_DIR": str(data), "PYTHONHASHSEED": seed}
    return subprocess.run([sys.executable, "-m", "rebrowse", "diff", *args], capture_output=True,
                          env=env, cwd=Path(__file__).resolve().parents[1], timeout=60,
                          check=False)


def test_a_real_process_exits_with_ci_codes_and_the_same_bytes_under_any_hash_seed(
        tmp_path, isolated):
    base = [_get("/api/menü", {"zeta": 1, "alpha": {"b": 1, "a": 1}, "naïve": [{"q": 1}]}),
            *_users({"id": 1, "email": "x"})]
    head = [_get("/api/menü", {"zeta": "1", "alpha": {}, "naïve": [{}]}),
            *_users({"id": 1, "email": None, "new": True})]
    base_har, head_har = _write(tmp_path, base, "base.har"), _write(tmp_path, head, "head.har")

    runs = [_run(str(base_har), str(head_har), seed=seed, data=isolated) for seed in ("1", "2")]
    bad = _run(str(base_har), str(tmp_path / "missing.har"), seed="0", data=isolated)

    assert [run.returncode for run in runs] == [1, 1], runs[0].stderr
    assert runs[0].stdout == runs[1].stdout
    report = json.loads(runs[0].stdout.decode("utf-8"))
    assert [(c["route"], c.get("field")) for c in report["changes"]] == [
        ("GET /api/menü", "$.alpha.a"),
        ("GET /api/menü", "$.alpha.b"),
        ("GET /api/menü", '$["naïve"][].q'),
        ("GET /api/menü", "$.zeta"),
        ("GET /api/users/{users_id}", "$.email"),
        ("GET /api/users/{users_id}", "$.new"),
    ]
    assert bad.returncode == 2 and "error" in json.loads(bad.stdout.decode("utf-8"))


@pytest.mark.parametrize("key", [
    "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "V1StGXR8_Z5jdHi6B-myT", "s3Kf_9xLq-2mPz7Rw8Tn",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl", "cus_9s6XKzkNRiz8i3",
])
def test_generated_ids_and_tokens_never_reach_the_report(key):
    changes = drift.compare(_saved({"k": {key: 1}}), _saved({"k": {key: "1"}}))

    assert [(c["kind"], c["field"]) for c in changes] == [("field_type", "$.k.*")]


def test_whole_floats_are_integers_and_widening_to_number_is_info():
    same = drift.compare(_saved({"n": 1}), _saved({"n": 1.0}))
    widened = drift.compare(_saved({"n": 3}), _saved({"n": 19.99}))

    assert same == []
    assert [(c["severity"], c["kind"], c["base"], c["head"]) for c in widened] == [
        ("info", "field_type", ["integer"], ["number"])]


def test_a_new_5xx_on_a_route_that_answered_is_breaking(tmp_path):
    base = [_get("/api/orders", {"n": 1}), _get("/api/gone", {"e": 1}, status=404)]
    head = [_get("/api/orders", {"n": 1}), _get("/api/orders", {"e": 1}, status=503),
            _get("/api/gone", {"e": 1}, status=500)]

    code, report = _diff(tmp_path, base, head)

    assert code == 1
    assert _rows(report) == [
        ("info", "status", "GET /api/gone", None, [404], [500]),
        ("breaking", "status", "GET /api/orders", None, [200], [200, 503]),
    ]


def test_domain_can_differ_per_side_and_bodies_are_counted(tmp_path):
    base = [_get("https://prod.example.com/api/a", {"x": 1})]
    head = [_entry("GET", "https://sso.example.org/login", mime="text/html", body="<p>"),
            _entry("GET", "https://staging.example.com/api/a", mime="application/json")]

    code, report = _diff(tmp_path, base, head, "-d", "prod.example.com",
                         "-d", "staging.example.com")

    assert (code, report["changes"]) == (0, [])
    assert (report["base"]["bodies"], report["head"]["bodies"]) == (1, 0)
