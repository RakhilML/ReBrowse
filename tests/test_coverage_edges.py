from __future__ import annotations

import json

from click.testing import CliRunner
from test_coverage import BUNDLE, SITE, _get, _js, _page, _report, _run
from test_har import _entry, _json_post, _write

from rebrowse.capture.store import load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.coverage import coverage_report
from rebrowse.models import CaptureResult


def test_trailing_slash_routes_are_covered_by_their_recording(tmp_path):
    report = _report(tmp_path, [
        _js('fetch("/api/orders/"); const ORDER = "/api/orders/" + id + "/";'),
        _get("/api/orders/", []),
        _get("/api/orders/42/", {"id": 42}),
    ])

    assert report["covered"] == [
        {"method": "GET", "path": "/api/orders/", "found_by": "call",
         "recorded_as": ["GET /api/orders/", "GET /api/orders/{orders_id}/"]}]
    assert (report["referenced"], report["unreferenced"]) == (1, 0)


def test_recorded_as_names_only_the_matching_routes_that_answered(tmp_path):
    report = _report(tmp_path, [
        _js('api.get("/items");'),
        _get("/api/items", {"items": []}),
        _get("/admin/items", {"error": "forbidden"}, status=403),
        _entry("POST", f"{SITE}/shop/items", post=_json_post({"q": 1}), body="{}"),
    ])

    assert report["covered"] == [{"method": "GET", "path": "/items", "found_by": "call",
                                  "recorded_as": ["GET /api/items", "POST /shop/items"]}]
    assert report["unrecorded"] == []


def test_error_statuses_of_every_matching_route_are_listed_once(tmp_path):
    report = _report(tmp_path, [
        _js('api.get("/items");'),
        _get("/api/items", {"error": "sign in"}, status=401),
        _get("/admin/items", {"error": "sign in"}, status=401),
        _entry("GET", f"{SITE}/shop/items", status=302, mime="", body=None),
    ])

    assert report["unrecorded"] == [
        {"method": "GET", "path": "/items", "effect": "read", "found_by": "call",
         "bundle": BUNDLE, "statuses": [302, 401]}]


def test_an_explicit_method_is_not_covered_by_another_write_method(tmp_path):
    report = _report(tmp_path, [
        _js('api.patch("/api/profile", p);'),
        _entry("PUT", f"{SITE}/api/profile", post=_json_post({"name": "a"}), body="{}"),
    ])

    assert report["unrecorded"] == [
        {"method": "PATCH", "path": "/api/profile", "effect": "write", "found_by": "call",
         "bundle": BUNDLE}]
    assert report["unreferenced"] == 1


def test_a_route_referenced_in_several_bundles_counts_once(tmp_path):
    first, second = f"{SITE}/static/a.js", f"{SITE}/static/b.js"
    report = _report(tmp_path, [
        _js("fetch(`/api/users/${id}`);", url=first),
        _js("fetch(`/api/users/${user.uid}`); fetch(`/api/users/${id}`);", url=second),
    ])

    assert report["bundles"] == 2
    assert report["referenced"] == 1
    assert report["unrecorded"] == [
        {"method": "GET", "path": "/api/users/{id}", "effect": "read", "found_by": "call",
         "bundle": first}]


def test_graphql_recorded_on_a_sibling_host_covers_the_operation(tmp_path):
    query = {"operationName": "GetCart", "query": "query GetCart { cart { id } }"}
    report = _report(tmp_path, [
        _js("const Q = gql`query GetCart { cart { id } }`;"),
        _entry("POST", "https://api.app.test/graphql", post=_json_post(query),
               body='{"data": {"cart": {}}}'),
    ])

    assert report["covered"] == [
        {"graphql": "query GetCart", "found_by": "document",
         "recorded_as": ["POST api.app.test/graphql [GraphQL query: GetCart]"]}]


def test_prefixes_come_from_sibling_hosts_and_any_method(tmp_path):
    report = _report(tmp_path, [
        _js('const A = "/rest/items"; const B = "/rpc/Orders.list";'),
        _get("https://api.app.test/rest/orders", {"orders": []}),
        _entry("POST", f"{SITE}/rpc/Cart.get", post=_json_post({}), body='{"cart": {}}'),
    ])

    assert [(row["path"], row["found_by"]) for row in report["unrecorded"]] == [
        ("/rest/items", "recorded_prefix"), ("/rpc/Orders.list", "recorded_prefix")]


def test_a_recording_without_api_calls_is_zero_percent_not_an_error(tmp_path):
    har = _write(tmp_path, [_page(), _js('fetch("/api/orders");')])

    result = _run(str(har), "--fail-under", "0")

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert (report["coverage"], report["unreferenced"], report["covered"]) == (0.0, 0, [])


def test_fail_under_compares_the_printed_percentage(tmp_path):
    har = str(_write(tmp_path, [
        _page(), _js('fetch("/api/orders"); fetch("/api/notes"); fetch("/api/labels");'),
        _get("/api/orders", []), _get("/api/notes", []),
    ]))

    at, above = _run(har, "--fail-under", "66.7"), _run(har, "--fail-under", "66.8")

    assert json.loads(at.stdout)["coverage"] == 66.7
    assert at.exit_code == 0
    assert above.exit_code == 1
    assert above.stderr.strip() == "[coverage] 66.7% is under --fail-under 66.8"


def test_bundle_source_never_reaches_the_output(tmp_path):
    secret = "sk_live_51Hbundlesecret"
    code = f'const KEY = "{secret}"; fetch("/api/orders"); // {secret}'
    har = _write(tmp_path, [_page(), _js(code), _get("/api/orders", [])])
    silent = _write(tmp_path, [_page(), _js(f'const KEY = "{secret}";')], "silent.har")

    results = [_run(str(har)), _run(str(silent))]

    assert [result.exit_code for result in results] == [0, 2]
    assert all(secret not in result.output for result in results)


def test_a_har_whose_scripts_are_all_cross_site_exits_2(tmp_path):
    har = _write(tmp_path, [_page(), _js('fetch("/api/orders");', url="https://cdn.other.net/a.js"),
                            _get("/api/orders", [])])

    result = _run(str(har))

    assert result.exit_code == 2
    assert "No JS bundles for app.test" in json.loads(result.stdout)["error"]


def test_a_saved_capture_with_only_analytics_scripts_exits_2():
    saved = save_capture(CaptureResult(domain="app.test", final_url=f"{SITE}/", js_bundles={
        "https://www.googletagmanager.com/gtm.js": 'fetch("/api/gtm/config");'}))

    result = _run(str(saved))

    assert result.exit_code == 2
    assert "analytics" in json.loads(result.stdout)["error"]



def test_generic_helpers_base_urls_and_trailing_slashes(tmp_path):
    bundle = ('const api = axios.create({baseURL: "/api/v2"});'
              'api.get(`/${resource}/${id}`); fetch("/api/teams");')
    har = _write(tmp_path, [
        _entry("GET", "https://app.test/", mime="text/html", body="<html></html>"),
        _entry("GET", "https://app.test/static/app.js", mime="application/javascript",
               body=bundle),
        _entry("GET", "https://app.test/api/teams/", body='{"teams": []}'),
        _entry("POST", "https://app.test/internal/stats", body="{}"),
    ])
    capture = load_traffic(har, every_script=True)

    report = coverage_report(capture)

    assert [row["path"] for row in report["covered"]] == ["/api/teams"]
    assert report["unrecorded"] == []
    assert report["unreferenced"] == 1


def test_fail_under_rejects_nan(tmp_path):
    result = CliRunner().invoke(main, ["coverage", str(tmp_path / "x.har"), "--fail-under", "nan"])
    assert result.exit_code == 2 and "number from 0 to 100" in result.output
