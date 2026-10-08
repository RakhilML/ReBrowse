from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner, Result
from test_har import CREDENTIAL_HEADERS, _entry, _json_post, _with_cookies, _write
from test_openapi import assert_valid

from rebrowse import config
from rebrowse.capture.har import HarError
from rebrowse.capture.store import har_files, load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.mock import MockServer, build_routes
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.orchestrator import pipeline
from rebrowse.store.skills import list_all_skills

SITE = "https://app.test"
BUNDLE = f"{SITE}/static/app.js"
APP_JS = 'function cart(){ return fetch("/api/cart"); } function list(){ return fetch("/api/orders"); }'
ROUTES = ["/api/cart", "/api/orders", "/api/orders/{orders_id}"]


def _page(site: str = SITE, path: str = "/") -> dict:
    return _entry("GET", f"{site}{path}", mime="text/html", body="<html></html>")


def _get(url: str, payload) -> dict:
    return _entry("GET", url if "://" in url else f"{SITE}{url}", body=json.dumps(payload))


def _js(code: str = APP_JS, url: str = BUNDLE) -> dict:
    return _entry("GET", url, mime="application/javascript", body=code)


def _checkout() -> list[dict]:
    return [_page(), _get("/api/cart", {"items": [{"sku": "a1", "qty": 1}], "total": 12})]


def _orders(total: bool = True) -> list[dict]:
    order = {"id": 1002, "total": 30} if total else {"id": 1002}
    return [_page(), _get("/api/orders", {"orders": [{"id": 1002, "total": 30}]}),
            _get("/api/orders/1002", order)]


def _suite(root: Path, files: dict[str, list[dict]]) -> Path:
    for name, entries in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        _write(root, entries, name)
    return root


def _e2e(root: Path) -> Path:
    return _suite(root, {"checkout/network.har": _checkout(), "orders/network.har": _orders()})


def _combined(tmp_path: Path) -> Path:
    return _write(tmp_path, [*_checkout(), *_orders()], "combined.har")


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def _relative(suite: Path) -> list[str]:
    return [path.relative_to(suite).as_posix() for path in har_files(suite)]


@pytest.fixture
def no_serving(monkeypatch) -> None:
    monkeypatch.setattr(MockServer, "serve_forever", lambda self, poll_interval=0.5: None)


@pytest.fixture
def stub_llm(monkeypatch) -> None:
    async def describe(endpoints):
        return [{**e, "description": f"stub: {e['method']} {e['url_template']}"} for e in endpoints]

    monkeypatch.setattr(pipeline, "describe_endpoints", describe)


def test_a_directory_reads_as_one_har_of_its_files_in_order(tmp_path):
    suite = _e2e(tmp_path / "test-results")
    combined = _combined(tmp_path)
    from_dir, from_har = tmp_path / "a.json", tmp_path / "b.json"

    result = _invoke("baseline", str(suite), "-o", str(from_dir))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["source"] == str(suite.resolve())
    assert _invoke("baseline", str(combined), "-o", str(from_har)).exit_code == 0
    assert from_dir.read_bytes() == from_har.read_bytes()
    document = _invoke("openapi", str(suite)).stdout
    assert document == _invoke("openapi", str(combined)).stdout
    assert sorted(json.loads(document)["paths"]) == ROUTES
    assert_valid(json.loads(document))


def test_renamed_or_reordered_files_give_the_same_baseline(tmp_path):
    first = _suite(tmp_path / "first", {"a/network.har": _checkout(), "b/network.har": _orders()})
    second = _suite(tmp_path / "second", {"a/network.har": _orders(), "z/network.har": _checkout()})

    assert _invoke("baseline", str(first)).stdout == _invoke("baseline", str(second)).stdout


def test_files_that_open_on_different_pages_give_the_same_baseline(tmp_path):
    checkout = [_page(path="/checkout"), *_checkout()[1:]]
    orders = [_page(path="/orders?tab=open"), *_orders()[1:]]
    first = _suite(tmp_path / "first", {"a/network.har": checkout, "b/network.har": orders})
    second = _suite(tmp_path / "second", {"a/network.har": orders, "b/network.har": checkout})

    baseline = _invoke("baseline", str(first)).stdout

    assert baseline == _invoke("baseline", str(second)).stdout
    assert json.loads(baseline)["final_url"] == f"{SITE}/"
    assert _invoke("openapi", str(first)).stdout == _invoke("openapi", str(second)).stdout


def test_the_site_is_the_first_html_page_by_relative_path(tmp_path):
    app = [_page(), _get("/api/cart", {"items": []})]
    admin = [_page("https://admin.app.test"),
             _get("https://admin.app.test/api/users", {"users": []})]

    one = _suite(tmp_path / "one", {"a.har": app, "b.har": admin})
    two = _suite(tmp_path / "two", {"a.har": admin, "b.har": app})

    assert load_traffic(one).domain == "app.test"
    assert load_traffic(two).domain == "admin.app.test"


def test_files_are_ordered_by_posix_relative_path_on_every_os(tmp_path):
    names = ("aZ/x.har", "a/x.har", "B/x.har", "a/Z.har")
    suite = _suite(tmp_path / "suite", {name: _checkout() for name in names})

    assert _relative(suite) == ["B/x.har", "a/Z.har", "a/x.har", "aZ/x.har"]


def test_har_files_at_any_depth_and_in_any_case_are_read_and_nothing_else(tmp_path):
    suite = _suite(tmp_path / "suite", {"one/two/three/network.har": _checkout(),
                                        "X.HAR": _orders()})
    for name in ("x.json", "trace.zip", "network.har.zip", "README.md"):
        (suite / name).write_text("not a har", encoding="utf-8")
    (suite / "folder.har").mkdir()

    assert _relative(suite) == ["X.HAR", "one/two/three/network.har"]
    assert sorted(route.display for route in build_routes(load_traffic(suite))) == ROUTES


def test_a_directory_with_one_har_reads_as_that_file(tmp_path):
    har = _write(tmp_path, _orders(), "orders.har")
    suite = _suite(tmp_path / "suite", {"orders/network.har": _orders()})

    for command in ("baseline", "openapi"):
        from_dir = _invoke(command, str(suite))
        assert from_dir.exit_code == 0, from_dir.output
        assert from_dir.stdout == _invoke(command, str(har)).stdout
        assert from_dir.stderr == f"[{command}] read 1 HAR file under {suite}\n"


def test_a_directory_without_har_files_is_an_input_error(tmp_path, fixture_site, hits, stub_llm):
    empty = tmp_path / "empty"
    (empty / "logs").mkdir(parents=True)
    (empty / "trace.zip").write_bytes(b"PK")
    out = tmp_path / "out.json"
    cases = {
        ("baseline", str(empty), "-o", str(out)): 1,
        ("openapi", str(empty), "-o", str(out)): 1,
        ("mock", str(empty), "-p", "0"): 1,
        ("import-har", str(empty)): 1,
        ("diff", str(empty), str(empty)): 2,
        ("contract", str(empty), "--against", fixture_site): 2,
        ("coverage", str(empty)): 2,
    }

    for args, code in cases.items():
        result = _invoke(*args)
        assert result.exit_code == code, (args, result.output)
        assert json.loads(result.stdout)["error"] == f"No .har files under {empty}", args

    assert not out.exists() and not hits
    assert list_all_skills() == []


@pytest.mark.parametrize("text, error", [
    ('{"log": {"entries": [{"request": ', "Cannot read {} as JSON: "),
    ('{"log": {}}', "{} is not a HAR file: log.entries is missing or not a list"),
])
def test_a_broken_har_among_valid_ones_fails_naming_it(tmp_path, fixture_site, hits, text,
                                                       error):
    items = [_page(fixture_site), _get(f"{fixture_site}/api/items", {"items": []})]
    suite = _suite(tmp_path / "suite", {"a/network.har": items, "c/network.har": items})
    broken = suite / "b" / "network.har"
    broken.parent.mkdir()
    broken.write_text(text, encoding="utf-8")
    out = tmp_path / "out.json"

    written = _invoke("baseline", str(suite), "-o", str(out))
    replayed = _invoke("contract", str(suite), "--against", fixture_site)

    assert written.exit_code == 1 and not out.exists()
    assert replayed.exit_code == 2 and not hits
    for result in (written, replayed):
        assert json.loads(result.stdout)["error"].startswith(error.format(broken))


def test_a_folder_that_cannot_be_listed_is_an_input_error(tmp_path, monkeypatch):
    suite = _suite(tmp_path / "suite", {"a/network.har": _orders(),
                                        "locked/network.har": _checkout()})
    scandir = os.scandir

    def refuse(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", refuse)

    with pytest.raises(HarError, match=r"Cannot list .*locked: Permission denied"):
        har_files(suite)


def test_domain_picks_the_site_across_files(tmp_path):
    suite = _suite(tmp_path / "suite", {
        "api.har": [_page(), _get("https://api.app.test/v1/orders", {"orders": []})],
        "vendor.har": [_get("https://cdn.vendor.test/api/config", {"flags": {}})],
        "web.har": [_page(), _get("/api/cart", {"items": []})],
    })

    capture = load_traffic(suite, domain="api.app.test")

    assert capture.domain == "api.app.test"
    assert [route.name for route in build_routes(capture)] == [
        "GET /v1/orders", "GET app.test/api/cart"]
    kept = _invoke("baseline", str(suite), "-d", "api.app.test")
    assert kept.exit_code == 0 and json.loads(kept.stdout)["domain"] == "api.app.test"
    documented = _invoke("openapi", str(suite), "-d", "api.app.test")
    assert documented.exit_code == 0 and "/v1/orders" in json.loads(documented.stdout)["paths"]
    missing = _invoke("baseline", str(suite), "-d", "nothere.test")
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"].startswith("No requests to nothere.test")


def test_credentials_in_any_file_stay_out(tmp_path):
    me = _with_cookies(_entry("GET", f"{SITE}/api/me?access_token=TOKEN_SECRET&fields=name",
                              headers=CREDENTIAL_HEADERS, body='{"name": "ann"}'))
    suite = _suite(tmp_path / "suite", {"a.har": _checkout(), "b.har": [_page(), me]})

    for command in ("baseline", "openapi"):
        result = _invoke(command, str(suite))
        assert result.exit_code == 0, result.output
        assert "/api/me" in result.stdout and "_SECRET" not in result.stdout


def test_coverage_spans_the_suite_and_reads_a_shared_bundle_once(tmp_path):
    suite = _suite(tmp_path / "suite", {
        "a.har": [_page(), _js(), _get("/api/orders", {"orders": []})],
        "b.har": [_page(), _js(), _get("/api/cart", {"items": []})],
    })

    whole = json.loads(_invoke("coverage", str(suite)).stdout)
    alone = json.loads(_invoke("coverage", str(suite / "a.har")).stdout)

    assert (whole["coverage"], whole["bundles"], whole["unrecorded"]) == (100.0, 1, [])
    assert alone["coverage"] == 50.0


def test_coverage_reads_every_bundle_in_the_suite_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_JS_BUNDLES", 1)
    admin = _js('function users(){ return fetch("/api/admin/users"); }', f"{SITE}/static/admin.js")
    suite = _suite(tmp_path / "suite", {
        "a.har": [_page(), _js(), _get("/api/orders", {"orders": []})],
        "b.har": [_page(), admin, _get("/api/cart", {"items": []})],
    })

    report = json.loads(_invoke("coverage", str(suite)).stdout)

    assert (report["bundles"], report["referenced"]) == (2, 3)
    assert list(load_traffic(suite).js_bundles) == [BUNDLE]


def test_a_script_in_several_files_keeps_the_first_files_text(tmp_path):
    suite = _suite(tmp_path / "suite", {"a.har": [_page(), _js('fetch("/api/new")')],
                                        "b.har": [_page(), _js('fetch("/api/old")')]})

    assert load_traffic(suite).js_bundles == {BUNDLE: 'fetch("/api/new")'}


def test_diff_compares_two_directories(tmp_path):
    base, same = _e2e(tmp_path / "base"), _e2e(tmp_path / "same")
    head = _suite(tmp_path / "head", {"checkout/network.har": _checkout(),
                                      "orders/network.har": _orders(total=False)})

    broken = _invoke("diff", str(base), str(head))

    assert broken.exit_code == 1, broken.output
    report = json.loads(broken.stdout)
    assert report["base"]["source"] == str(base.resolve())
    assert report["head"]["source"] == str(head.resolve())
    assert [(c["severity"], c["kind"], c["route"], c.get("field")) for c in report["changes"]] == [
        ("breaking", "field_removed", "GET /api/orders/{orders_id}", "$.total")]
    assert broken.stderr == (f"[diff] read 2 HAR files under {base}\n"
                             f"[diff] read 2 HAR files under {head}\n")
    assert _invoke("diff", str(base), str(same)).exit_code == 0


def test_contract_replays_a_directory_as_its_combined_har(tmp_path, fixture_site, hits):
    first = [_page(fixture_site),
             _get(f"{fixture_site}/api/items", {"items": [{"id": 0}], "next": "x"})]
    second = [_page(fixture_site),
              _get(f"{fixture_site}/api/search?q=a", {"results": [{"id": 1, "title": "t"}]}),
              _entry("POST", f"{fixture_site}/api/notes", post=_json_post({"text": "hi"}),
                     body='{"ok": true}')]
    suite = _suite(tmp_path / "suite", {"a.har": first, "b.har": second})
    combined = _write(tmp_path, [*first, *second], "combined.har")

    from_dir = _invoke("contract", str(suite), "--against", fixture_site)
    sent = dict(hits)
    hits.clear()
    from_har = _invoke("contract", str(combined), "--against", fixture_site)

    assert from_dir.exit_code == from_har.exit_code == 0, from_dir.output
    report, expected = json.loads(from_dir.stdout), json.loads(from_har.stdout)
    assert report.pop("source") == str(suite.resolve())
    expected.pop("source")
    assert report == expected
    assert (report["replayed"], len(report["skipped"])) == (2, 1)
    assert sent == dict(hits) == {("GET", "/api/items"): 1, ("GET", "/api/search"): 1}
    assert from_dir.stderr == f"[contract] read 2 HAR files under {suite}\n"


def test_mock_serves_the_routes_of_every_file(tmp_path, no_serving):
    suite = _e2e(tmp_path / "suite")

    def table(source: Path) -> list[tuple[str, int]]:
        return [(route.name, len(route.recordings)) for route in build_routes(load_traffic(source))]

    assert table(suite) == table(_combined(tmp_path))
    result = _invoke("mock", str(suite), "-p", "0")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"].startswith("http://127.0.0.1:")
    assert payload["source"] == str(suite.resolve())
    assert [row["path"] for row in payload["routes"]] == ROUTES


def test_import_har_learns_one_skill_from_a_directory(tmp_path, stub_llm):
    suite = _e2e(tmp_path / "suite")

    result = _invoke("import-har", str(suite))

    assert result.exit_code == 0, result.output
    learned = json.loads(result.stdout)
    assert learned["source"] == str(suite.resolve())
    assert sorted(urlsplit(e["url"]).path for e in learned["endpoints"]) == ROUTES
    again = json.loads(_invoke("import-har", str(suite)).stdout)
    assert (again["skill_id"], again["replaced"]) == (learned["skill_id"], True)
    assert len(list_all_skills()) == 1


@pytest.mark.parametrize("command, args", [
    ("baseline", ()), ("openapi", ()), ("mock", ("-p", "0")), ("coverage", ()),
    ("import-har", ()),
])
def test_each_command_notes_the_files_it_read_on_stderr(tmp_path, no_serving, stub_llm,
                                                        command, args):
    suite = _suite(tmp_path / "suite", {"a.har": [*_checkout(), _js()], "b.har": _orders()})

    result = _invoke(command, str(suite), *args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)
    note = f"[{command}] read 2 HAR files under {suite}"
    assert result.stderr.splitlines().count(note) == 1


def test_a_directory_wins_over_a_saved_capture_of_the_same_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_capture(CaptureResult(domain="app.test", final_url=f"{SITE}/", requests=[RawRequest(
        url=f"{SITE}/api/saved", method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body="{}")]))

    def baseline_paths() -> list[str]:
        result = _invoke("baseline", "app.test")
        assert result.exit_code == 0, result.output
        return [urlsplit(req["url"]).path for req in json.loads(result.stdout)["requests"]]

    assert baseline_paths() == ["/api/saved"]
    _suite(tmp_path / "app.test", {"network.har": _orders()})
    assert baseline_paths() == ["/api/orders", "/api/orders/1002"]


def test_the_first_html_page_decides_even_after_a_file_without_one(tmp_path):
    vendor = [_get("https://cdn.vendor.test/api/config", {"flags": {}})]
    suite = _suite(tmp_path / "suite", {"a.har": vendor, "b.har": _checkout()})

    result = _invoke("baseline", str(suite))

    assert load_traffic(suite).domain == "app.test"
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["domain"] == "app.test"


def test_openapi_reads_a_directory_named_like_a_skill(tmp_path, monkeypatch, stub_llm):
    monkeypatch.chdir(tmp_path)
    assert _invoke("import-har", str(_write(tmp_path, _checkout(), "cart.har"))).exit_code == 0
    assert list(json.loads(_invoke("openapi", "app.test").stdout)["paths"]) == ["/api/cart"]
    _suite(tmp_path / "app.test", {"orders/network.har": _orders()})

    result = _invoke("openapi", "app.test")

    assert result.exit_code == 0, result.output
    assert sorted(json.loads(result.stdout)["paths"]) == ROUTES[1:]
    assert result.stderr == "[openapi] read 1 HAR file under app.test\n"


def test_import_har_stores_nothing_from_a_directory_with_a_broken_har(tmp_path, stub_llm):
    suite = _e2e(tmp_path / "suite")
    broken = suite / "crashed" / "network.har"
    broken.parent.mkdir()
    broken.write_text('{"log": {"entries": [', encoding="utf-8")

    result = _invoke("import-har", str(suite))

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"].startswith(f"Cannot read {broken} as JSON: ")
    assert list_all_skills() == []


def test_import_har_domain_spans_the_files(tmp_path, stub_llm):
    suite = _suite(tmp_path / "suite", {
        "api.har": [_page(), _get("https://api.app.test/v1/orders", {"orders": []})],
        "vendor.har": [_get("https://cdn.vendor.test/api/config", {"flags": {}})],
    })

    result = _invoke("import-har", str(suite), "-d", "api.app.test")

    assert result.exit_code == 0, result.output
    learned = json.loads(result.stdout)
    assert learned["domain"] == "api.app.test"
    assert "https://api.app.test/v1/orders" in [e["url"] for e in learned["endpoints"]]


def test_directory_symlinks_are_not_followed(tmp_path):
    outside = _suite(tmp_path / "outside", {"network.har": _checkout()})
    suite = _suite(tmp_path / "suite", {"a/network.har": _orders()})
    try:
        (suite / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"cannot create a directory symlink here: {e}")

    assert _relative(suite) == ["a/network.har"]
