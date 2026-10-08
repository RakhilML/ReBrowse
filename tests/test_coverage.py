from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner, Result
from test_har import _entry, _json_post, _write

from rebrowse import config
from rebrowse.capture.store import save_capture
from rebrowse.cli import main
from rebrowse.coverage import coverage_report
from rebrowse.llm import client as llm
from rebrowse.models import CaptureResult, RawRequest

SITE = "https://app.test"
BUNDLE = f"{SITE}/static/app.js"
APP_JS = """
function list(){ return fetch("/api/orders"); }
function one(id){ return fetch(`/api/orders/${id}`); }
function refund(order){ return axios.post("/api/orders/refund", order); }
const REPORTS = "/api/reports";
"""
GRAPHQL_JS = r"""
const GET_CART = gql`
  query GetCart($id: ID!) {
    cart(id: $id) { ...CartFields }
  }
  fragment CartFields on Cart { id }
`;
const REMOVE = "\nmutation RemoveItem{removeItem{id}}";
const CHECKOUT = {kind:"Document",definitions:[{kind:"OperationDefinition",operation:"mutation",name:{kind:"Name",value:"Checkout"},selectionSet:{kind:"SelectionSet",selections:[]}}]};
function fail(status){ throw new Error("query failed (" + status + ")"); }
function failed(res){ throw new Error(`query failed (${res.status})`); }
"""


def _page() -> dict:
    return _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")


def _js(code: str, url: str = BUNDLE) -> dict:
    return _entry("GET", url, mime="application/javascript", body=code)


def _get(path: str, payload=None, **kwargs) -> dict:
    url = path if "://" in path else f"{SITE}{path}"
    return _entry("GET", url, body=json.dumps({} if payload is None else payload), **kwargs)


def _orders_har(tmp_path: Path) -> Path:
    return _write(tmp_path, [_page(), _js(APP_JS), _get("/api/orders", []),
                             _get("/api/orders/42", {"id": 42})])


def _run(*args: str) -> Result:
    return CliRunner().invoke(main, ["coverage", *args])


def _report(tmp_path: Path, entries: list) -> dict:
    result = _run(str(_write(tmp_path, [_page(), *entries])))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_reports_the_calls_the_recording_left_out(tmp_path):
    har = _orders_har(tmp_path)

    result = _run(str(har))

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["source"] == str(har.resolve())
    assert {key: report[key] for key in ("domain", "bundles", "referenced", "coverage")} == {
        "domain": "app.test", "bundles": 1, "referenced": 4, "coverage": 50.0}
    assert report["unreferenced"] == 0
    assert report["unrecorded"] == [
        {"method": "POST", "path": "/api/orders/refund", "effect": "write", "found_by": "call",
         "bundle": BUNDLE},
        {"method": "GET", "path": "/api/reports", "effect": "read", "found_by": "string_literal",
         "bundle": BUNDLE},
    ]
    assert report["covered"] == [
        {"method": "GET", "path": "/api/orders", "found_by": "call",
         "recorded_as": ["GET /api/orders"]},
        {"method": "GET", "path": "/api/orders/{id}", "found_by": "call",
         "recorded_as": ["GET /api/orders/{orders_id}"]},
    ]


def test_a_guessed_get_matches_any_method_and_an_explicit_method_its_own(tmp_path):
    report = _report(tmp_path, [
        _js('session.delete("/api/sessions"); const NOTES = "/api/notes";'),
        _get("/api/sessions", {"active": True}),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body="{}"),
    ])

    assert report["unrecorded"] == [
        {"method": "DELETE", "path": "/api/sessions", "effect": "destructive", "found_by": "call",
         "bundle": BUNDLE}]
    assert report["covered"] == [
        {"method": "GET", "path": "/api/notes", "found_by": "string_literal",
         "recorded_as": ["POST /api/notes"]}]
    assert report["unreferenced"] == 1


def test_suffixes_and_trailing_slashes_match_but_literals_must_be_equal(tmp_path):
    report = _report(tmp_path, [
        _js('api.get("/orders/summary"); const USER = "/api/users/" + id; '
            'const ME = "/api/users/me";'),
        _get("/api/v2/orders/summary", {"total": 3}),
        _get("/api/users/7", {"id": 7}),
    ])

    assert {row["path"]: row["recorded_as"] for row in report["covered"]} == {
        "/api/users/": ["GET /api/users/7"],
        "/orders/summary": ["GET /api/v2/orders/summary"],
    }
    assert [(row["method"], row["path"]) for row in report["unrecorded"]] == [
        ("GET", "/api/users/me")]


def test_write_calls_with_a_template_literal_need_their_own_method(tmp_path):
    report = _report(tmp_path, [
        _js("api.delete(`/api/items/${item.id}`); axios.post(`/api/orders/${id}/refund`, o);"),
        _get("/api/items/7", {"id": 7}),
        _get("/api/orders/9/refund", {"refundable": True}),
    ])

    assert report["unrecorded"] == [
        {"method": "DELETE", "path": "/api/items/{id}", "effect": "destructive",
         "found_by": "call", "bundle": BUNDLE},
        {"method": "POST", "path": "/api/orders/{id}/refund", "effect": "write",
         "found_by": "call", "bundle": BUNDLE},
    ]
    assert (report["covered"], report["coverage"], report["unreferenced"]) == ([], 0.0, 2)


def test_literal_ids_are_covered_by_their_own_recording(tmp_path):
    report = _report(tmp_path, [
        _js('fetch("/api/reports/2024"); fetch("/api/products/12345"); '
            'fetch("/api/users/me"); fetch("/api/orders/2024");'),
        _get("/api/reports/2024", {"year": 2024}),
        _get("/api/products/12345", {"id": 12345}),
        _get("/api/users/7", {"id": 7}),
        _get("/api/orders/summary", {"total": 1}),
    ])

    assert {row["path"]: row["recorded_as"] for row in report["covered"]} == {
        "/api/products/12345": ["GET /api/products/{products_id}"],
        "/api/reports/2024": ["GET /api/reports/{reports_id}"],
    }
    assert [row["path"] for row in report["unrecorded"]] == ["/api/orders/2024", "/api/users/me"]


def test_legacy_endpoints_with_dotted_paths_are_found(tmp_path):
    report = _report(tmp_path, [
        _js('fetch("/ajax/get_orders.php"); fetch("/ajax/get_cart.php"); '
            '$.post("/ajax/save_cart.php", form); const A = "/ajax/orders.php"; '
            'const B = "/services/OrderService.asmx/GetOrders";'),
        _get("/ajax/get_orders.php", {"orders": []}),
        _get("/services/OrderService.asmx/GetCustomers", {"d": []}),
    ])

    assert [(row["method"], row["path"], row["effect"], row["found_by"])
            for row in report["unrecorded"]] == [
        ("GET", "/ajax/get_cart.php", "read", "call"),
        ("GET", "/ajax/orders.php", "read", "recorded_prefix"),
        ("POST", "/ajax/save_cart.php", "write", "call"),
        ("GET", "/services/OrderService.asmx/GetOrders", "read", "recorded_prefix"),
    ]
    assert report["covered"] == [{"method": "GET", "path": "/ajax/get_orders.php",
                                  "found_by": "call", "recorded_as": ["GET /ajax/get_orders.php"]}]
    assert report["unreferenced"] == 1


def test_every_same_site_script_in_a_har_is_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_JS_BUNDLES", 1)
    monkeypatch.setattr(config, "MAX_BUNDLE_CHARS", 40)
    big = 'fetch("/api/orders");' + " " * 40

    report = _report(tmp_path, [_js(big, url=f"{SITE}/static/main.js"),
                                _js('fetch("/api/reports");'), _get("/api/orders", [])])

    assert (report["bundles"], report["referenced"], report["coverage"]) == (2, 2, 50.0)


def test_a_call_to_a_sibling_api_host_covers_a_relative_path(tmp_path):
    report = _report(tmp_path, [_js('const ITEMS = "/v1/items";'),
                                _get("https://api.app.test/v1/items", {"items": []})])

    assert report["covered"] == [{"method": "GET", "path": "/v1/items", "found_by": "string_literal",
                                  "recorded_as": ["GET api.app.test/v1/items"]}]
    assert report["coverage"] == 100.0


def test_a_route_recorded_only_with_errors_or_redirects_is_unrecorded(tmp_path):
    report = _report(tmp_path, [
        _js('const ADMIN = "/api/admin"; const BILLING = "/api/billing";'),
        _get("/api/admin", {"error": "sign in"}, status=401),
        _entry("GET", f"{SITE}/api/billing", status=302, mime="", body=None),
        _entry("GET", f"{SITE}/api/billing", status=500, body="{}"),
    ])

    assert [(row["path"], row["statuses"]) for row in report["unrecorded"]] == [
        ("/api/admin", [401]), ("/api/billing", [302, 500])]
    assert (report["covered"], report["coverage"]) == ([], 0.0)


def test_graphql_operations_in_documents_and_compiled_asts(tmp_path):
    query = {"operationName": "GetCart", "variables": {"id": 1},
             "query": "query GetCart($id: ID!) { cart(id: $id) { id } }"}
    report = _report(tmp_path, [
        _js(GRAPHQL_JS),
        _entry("POST", f"{SITE}/graphql", post=_json_post(query), body='{"data": {"cart": {}}}'),
    ])

    assert report["covered"] == [{"graphql": "query GetCart", "found_by": "document",
                                  "recorded_as": ["POST /graphql [GraphQL query: GetCart]"]}]
    assert report["unrecorded"] == [
        {"graphql": "mutation Checkout", "effect": "write", "found_by": "compiled",
         "bundle": BUNDLE},
        {"graphql": "mutation RemoveItem", "effect": "destructive", "found_by": "document",
         "bundle": BUNDLE},
    ]
    assert (report["referenced"], report["coverage"]) == (3, 33.3)
    assert "failed" not in json.dumps(report)


def test_recorded_json_routes_lend_their_first_segment_as_a_prefix(tmp_path):
    report = _report(tmp_path, [
        _js('const A = "/rest/invoices"; const B = `/rest/invoices/${invoice.id}`; '
            'const C = "/en/about";'),
        _get("/rest/orders", {"orders": []}),
        _entry("GET", f"{SITE}/en/home", mime="text/html", body="<html></html>"),
    ])

    assert [(row["path"], row["found_by"]) for row in report["unrecorded"]] == [
        ("/rest/invoices", "recorded_prefix"), ("/rest/invoices/{id}", "recorded_prefix")]
    assert report["referenced"] == 2


def test_telemetry_paths_are_not_references(tmp_path):
    report = _report(tmp_path, [_js('fetch("/api/orders"); const T = "/api/telemetry";'),
                                _get("/api/orders", [])])

    assert [row["path"] for row in report["covered"]] == ["/api/orders"]
    assert (report["referenced"], report["unrecorded"]) == (1, [])


def test_third_party_bundles_in_a_saved_capture_are_not_scanned():
    saved = save_capture(CaptureResult(domain="app.test", final_url=f"{SITE}/", js_bundles={
        BUNDLE: 'fetch("/api/orders");',
        "https://www.googletagmanager.com/gtm.js": 'const C = "/api/gtm/config";',
    }))

    result = _run(str(saved))

    report = json.loads(result.stdout)
    assert report["bundles"] == 1
    assert report["unrecorded"] == [
        {"method": "GET", "path": "/api/orders", "effect": "read", "found_by": "call",
         "bundle": BUNDLE}]


def test_bundle_urls_lose_their_query_and_fragment(tmp_path):
    report = _report(tmp_path, [_js('const R = "/api/reports";',
                                    url=f"{BUNDLE}?v=123&token=abc#frag")])

    assert report["unrecorded"][0]["bundle"] == BUNDLE
    assert "v=123" not in json.dumps(report)


def test_fail_under_gates_on_the_percentage(tmp_path):
    har = str(_orders_har(tmp_path))

    under = _run(har, "--fail-under", "60")
    at = _run(har, "--fail-under", "50")
    invalid = _run(har, "--fail-under", "101")

    assert under.exit_code == 1
    assert json.loads(under.stdout)["unrecorded"] == json.loads(at.stdout)["unrecorded"]
    assert under.stderr.strip() == "[coverage] 50% is under --fail-under 60"
    assert at.exit_code == 0 and at.stderr == ""
    assert invalid.exit_code == 2 and "--fail-under" in invalid.stderr


def test_inputs_without_bundles_or_references_exit_2(tmp_path):
    har = _orders_har(tmp_path)
    baseline = tmp_path / "api-baseline.json"
    CliRunner().invoke(main, ["baseline", str(har), "-o", str(baseline)])
    silent = _write(tmp_path, [_page(), _js("console.log('ready');"), _get("/api/orders", [])],
                    "silent.har")

    for source, message in ((baseline, "JS bundles"), (silent, "reference no API route"),
                            (tmp_path / "missing.har", "neither a file"),
                            ("nowhere.test", "neither a file")):
        result = _run(str(source))
        assert result.exit_code == 2, result.output
        assert message in json.loads(result.stdout)["error"]


def test_a_host_resolves_its_newest_saved_capture():
    def capture(bundle: str) -> CaptureResult:
        return CaptureResult(domain="app.test", final_url=f"{SITE}/", js_bundles={BUNDLE: bundle},
                             requests=[RawRequest(
                                 url=f"{SITE}/api/orders", method="GET", response_status=200,
                                 response_headers={"content-type": "application/json"},
                                 response_body="[]")])

    save_capture(capture('fetch("/api/old");'),
                 config.CAPTURES_DIR / "app.test-20261001-000000.json")
    newest = save_capture(capture('fetch("/api/orders");'),
                          config.CAPTURES_DIR / "app.test-20261002-000000.json")

    result = _run("app.test")

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert (report["source"], report["coverage"]) == (str(newest.resolve()), 100.0)


def test_runs_offline_without_an_llm_and_gives_the_same_bytes(tmp_path, monkeypatch, isolated):
    har = str(_write(tmp_path, [_page(), _js(APP_JS + GRAPHQL_JS), _get("/api/orders", []),
                                _get("/api/orders/42", {"id": 42})]))

    def refuse(*args, **kwargs):
        raise AssertionError("coverage reached for the network or an LLM")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(llm, "_post", refuse)
    monkeypatch.setattr(llm, "_dspy_call", refuse)

    def snapshot() -> dict:
        return {path: path.read_bytes() if path.is_file() else None
                for path in sorted(isolated.rglob("*"))}

    before = snapshot()
    runs = [_run(har) for _ in range(2)]

    assert [run.exit_code for run in runs] == [0, 0], runs[0].output
    assert runs[0].stdout_bytes == runs[1].stdout_bytes
    assert snapshot() == before


def test_the_python_api_leaves_out_the_source():
    capture = CaptureResult(domain="app.test", final_url=f"{SITE}/",
                            js_bundles={BUNDLE: 'const R = "/api/reports";'})

    report = coverage_report(capture)

    assert "source" not in report and report["referenced"] == 1


def test_group_help_lists_coverage():
    result = CliRunner().invoke(main, ["--help"])

    assert "coverage <source> API calls the frontend's JS makes" in result.output


def test_a_304_answer_counts_as_recorded(tmp_path):
    report = _report(tmp_path, [_js('fetch("/api/orders");'),
                                _entry("GET", f"{SITE}/api/orders", status=304, mime="", body=None)])

    assert report["covered"] == [{"method": "GET", "path": "/api/orders", "found_by": "call",
                                  "recorded_as": ["GET /api/orders"]}]


def test_graphql_operations_recorded_by_get_or_only_with_errors(tmp_path):
    me = {"operationName": "Me", "query": "query Me { me { id } }"}
    report = _report(tmp_path, [
        _js("gql`query Feed { feed { id } }`; gql`query Me { me { id } }`;"),
        _get("/graphql?operationName=Feed&query=query%20Feed%20%7B%20feed%20%7B%20id%20%7D%20%7D",
             {"data": {}}),
        _entry("POST", f"{SITE}/graphql", post=_json_post(me), status=401, body='{"errors": []}'),
    ])

    assert [row["graphql"] for row in report["covered"]] == ["query Feed"]
    assert report["unrecorded"] == [{"graphql": "query Me", "effect": "read",
                                     "found_by": "document", "bundle": BUNDLE,
                                     "statuses": [401]}]


def test_explicit_methods_need_their_own_recording_and_sort_after_the_path(tmp_path):
    report = _report(tmp_path, [
        _js('api.put("/api/notes", n); api.post("/api/notes", n); const L = "/api/labels"; '
            "gql`mutation AddNote($t: String) { addNote(t: $t) { id } }`;"),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body="{}"),
    ])

    assert [row.get("graphql") or (row["method"], row["path"]) for row in report["unrecorded"]] == [
        ("GET", "/api/labels"), ("PUT", "/api/notes"), "mutation AddNote"]
    assert report["covered"] == [{"method": "POST", "path": "/api/notes", "found_by": "call",
                                  "recorded_as": ["POST /api/notes"]}]


def test_a_trailing_slash_reference_needs_a_segment_after_it(tmp_path):
    report = _report(tmp_path, [_js('const USER = "/api/users/" + id;'),
                                _get("/api/users", [])])

    assert [row["path"] for row in report["unrecorded"]] == ["/api/users/"]
    assert report["unreferenced"] == 1


def test_unreferenced_counts_only_answered_routes_that_are_not_html(tmp_path):
    report = _report(tmp_path, [
        _js('fetch("/api/orders");'),
        _get("/api/orders", []),
        _get("/api/runtime/built", {"ok": True}),
        _get("/api/broken", {}, status=500),
        _entry("GET", f"{SITE}/en/home", mime="text/html", body="<html></html>"),
    ])

    assert report["unreferenced"] == 1


def test_prefixes_need_an_answered_json_route_with_a_literal_first_segment(tmp_path):
    report = _report(tmp_path, [
        _js('fetch("/api/orders"); const A = "/legacy/invoices"; const B = "/orders/archive"; '
            'const C = "/12345/notes";'),
        _get("/api/orders", []),
        _get("/legacy/orders", {"error": "sign in"}, status=401),
        _get("/orders", []),
        _get("/12345/items", []),
    ])

    assert report["referenced"] == 1
    assert [row["path"] for row in report["covered"]] == ["/api/orders"]


def test_domain_picks_the_site_in_a_har(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", "https://other.test/", mime="text/html", body="<html></html>"),
        _js('fetch("/api/other");', url="https://other.test/o.js"),
        _page(), _js('fetch("/api/orders");'), _get("/api/orders", []),
    ])

    default, picked = _run(str(har)), _run(str(har), "--domain", "app.test")

    assert json.loads(default.stdout)["domain"] == "other.test"
    report = json.loads(picked.stdout)
    assert (picked.exit_code, report["domain"], report["coverage"]) == (0, "app.test", 100.0)


def test_the_python_api_raises_without_first_party_bundles():
    capture = CaptureResult(domain="app.test", final_url=f"{SITE}/", js_bundles={
        "https://www.googletagmanager.com/gtm.js": 'fetch("/api/gtm/config");'})

    with pytest.raises(ValueError, match="JS bundles") as raised:
        coverage_report(capture)
    assert "analytics" in str(raised.value) and "baseline" not in str(raised.value)
