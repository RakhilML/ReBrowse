from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_har import _entry, _write
from test_recording_dirs import _checkout, _combined, _e2e, _get, _invoke, _orders, _page, _suite

from rebrowse.capture.store import har_files, load_traffic
from rebrowse.cli import main


def _empty_har(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": []}}), encoding="utf-8")
    return path


def test_a_baseline_written_from_a_directory_reads_back_as_that_directory(tmp_path):
    suite = _e2e(tmp_path / "test-results")
    committed = tmp_path / "api-baseline.json"

    assert _invoke("baseline", str(suite), "-o", str(committed)).exit_code == 0

    clean = _invoke("diff", str(committed), str(suite))
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["changes"] == []
    assert clean.stderr == f"[diff] read 2 HAR files under {suite}\n"
    assert _invoke("openapi", str(committed)).stdout == _invoke("openapi", str(suite)).stdout


def test_setup_calls_every_test_repeats_are_kept_once(tmp_path):
    one = _write(tmp_path, _orders(), "one.har")
    suite = _suite(tmp_path / "suite", {f"test-{n}/network.har": _orders() for n in range(3)})

    from_dir = _invoke("baseline", str(suite))

    assert from_dir.exit_code == 0, from_dir.output
    assert from_dir.stdout == _invoke("baseline", str(one)).stdout
    assert _invoke("openapi", str(suite)).stdout == _invoke("openapi", str(one)).stdout


def test_a_har_without_entries_among_others_adds_nothing(tmp_path):
    suite = _e2e(tmp_path / "suite")
    expected = _invoke("baseline", str(_combined(tmp_path))).stdout
    _empty_har(suite / "skipped" / "network.har")

    result = _invoke("baseline", str(suite))

    assert result.exit_code == 0, result.output
    assert result.stdout == expected
    assert result.stderr == f"[baseline] read 3 HAR files under {suite}\n"


def test_a_directory_of_hars_without_entries_is_an_input_error(tmp_path, fixture_site, hits):
    suite = tmp_path / "suite"
    for name in ("a/network.har", "b/network.har"):
        _empty_har(suite / name)
    out = tmp_path / "out.json"
    cases = {
        ("baseline", str(suite), "-o", str(out)): 1,
        ("openapi", str(suite), "-o", str(out)): 1,
        ("diff", str(suite), str(suite)): 2,
        ("contract", str(suite), "--against", fixture_site): 2,
        ("coverage", str(suite)): 2,
    }

    for args, code in cases.items():
        result = _invoke(*args)
        assert result.exit_code == code, (args, result.output)
        assert json.loads(result.stdout)["error"], args

    assert not out.exists() and not hits


def test_the_current_directory_and_a_trailing_separator_read_the_same(tmp_path, monkeypatch):
    suite = _e2e(tmp_path / "test-results")
    expected = _invoke("baseline", str(suite.resolve())).stdout
    monkeypatch.chdir(suite)

    here = _invoke("baseline", ".")

    assert here.exit_code == 0, here.output
    assert here.stdout == expected
    assert here.stderr == "[baseline] read 2 HAR files under .\n"
    monkeypatch.chdir(tmp_path)
    slashed = _invoke("baseline", "test-results/")
    assert slashed.stdout == expected
    assert slashed.stderr == f"[baseline] read 2 HAR files under {Path('test-results')}\n"


def test_a_har_saved_with_a_byte_order_mark_is_read(tmp_path):
    suite = _e2e(tmp_path / "suite")
    expected = _invoke("baseline", str(suite)).stdout
    har = suite / "orders" / "network.har"
    har.write_text(har.read_text(encoding="utf-8"), encoding="utf-8-sig")

    assert _invoke("baseline", str(suite)).stdout == expected


def test_a_har_in_another_encoding_fails_naming_it(tmp_path):
    suite = _e2e(tmp_path / "suite")
    utf16 = suite / "orders" / "network.har"
    utf16.write_text(utf16.read_text(encoding="utf-8"), encoding="utf-16")
    out = tmp_path / "out.json"

    result = _invoke("baseline", str(suite), "-o", str(out))

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"].startswith(f"Cannot read {utf16} as JSON: ")
    assert not out.exists()


def test_coverage_of_a_suite_without_first_party_js_is_an_input_error(tmp_path):
    suite = _e2e(tmp_path / "suite")

    result = _invoke("coverage", str(suite))

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]


@pytest.mark.parametrize("domain", ["api.app.test", "https://api.app.test", "API.app.test:443"])
def test_domain_spellings_pick_the_same_site_across_files(tmp_path, domain):
    suite = _suite(tmp_path / "suite", {
        "a.har": [_page(), _get("/api/cart", {"items": []})],
        "b.har": [_page(), _get("https://api.app.test/v1/orders", {"orders": []})],
    })

    result = _invoke("baseline", str(suite), "-d", domain)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["domain"] == "api.app.test"
    assert load_traffic(suite, domain="api.app.test").domain == "api.app.test"


def test_a_suite_with_no_html_page_takes_the_first_files_first_host(tmp_path):
    suite = _suite(tmp_path / "suite", {
        "b.har": [_get("/api/cart", {"items": []})],
        "a.har": [_get("https://api.app.test/v1/orders", {"orders": []})],
    })

    assert load_traffic(suite).domain == "api.app.test"
    assert json.loads(_invoke("baseline", str(suite)).stdout)["domain"] == "api.app.test"


def test_a_file_named_like_a_har_inside_a_har_named_folder_is_read(tmp_path):
    suite = _suite(tmp_path / "suite", {"run.har/network.har": _checkout()})

    result = _invoke("baseline", str(suite))

    assert result.exit_code == 0, result.output
    assert result.stderr == f"[baseline] read 1 HAR file under {suite}\n"


@pytest.mark.parametrize("command", [["baseline", ""], ["coverage", " "], ["diff", "", ""],
                                     ["mock", ""], ["openapi", ""]])
def test_an_empty_source_never_reads_the_current_directory(tmp_path, monkeypatch, command):
    (tmp_path / "fixtures").mkdir()
    _write(tmp_path / "fixtures", [_entry("GET", "https://app.test/", mime="text/html",
                                          body="<p>"),
                                   _entry("GET", "https://app.test/api/a", body="{}")])
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, command)

    assert result.exit_code in (1, 2)
    assert "empty" in json.loads(result.stdout)["error"]
    assert "read 1 HAR file" not in result.stderr


def test_dot_folders_node_modules_and_appledouble_files_are_skipped(tmp_path):
    for folder in (".cache", "node_modules/pkg", "__MACOSX"):
        (tmp_path / folder).mkdir(parents=True)
        _write(tmp_path / folder, [_entry("GET", "https://other.test/api/x", body="{}")])
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "._network.har").write_bytes(b"\x00\x05\x16\x07")
    _write(tmp_path / "suite", [_entry("GET", "https://app.test/api/a", body="{}")])

    assert [p.relative_to(tmp_path).as_posix() for p in har_files(tmp_path)] == [
        "suite/session.har"]
