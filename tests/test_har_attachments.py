from __future__ import annotations

import copy
import hashlib
import json
import struct
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner
from test_har import _entry, _json_post, _write
from test_mock import _gql, _serving
from test_recording_dirs import _checkout, _invoke, _orders, _relative

from rebrowse import config
from rebrowse.capture.har import HarError, load_har
from rebrowse.capture.store import har_files, load_traffic
from rebrowse.cli import main
from rebrowse.mock import build_routes
from rebrowse.orchestrator import pipeline
from rebrowse.safety import REDACTED
from rebrowse.store.skills import list_all_skills

SITE = "https://shop.test"
FORM = "application/x-www-form-urlencoded"
BUNDLE = f"{SITE}/static/app.js"
APP_JS = ('function list(){ return fetch("/api/orders"); } '
          'function admin(){ return fetch("/api/admin"); }')
ORDERS = {"orders": [{"id": 1002, "total": 30}]}
EXTENSIONS = {"application/json": "json", "text/html": "html", "application/javascript": "js"}
MISSING = "; keep the files Playwright wrote next to the HAR with it"
UNSAFE = "is not a plain file name in the HAR's folder"


def _page() -> dict:
    return _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")


def _form(text: str) -> dict:
    params = [{"name": name, "value": value}
              for name, value in (pair.split("=") for pair in text.split("&"))]
    return {"mimeType": FORM, "text": text, "params": params}


def _traffic() -> list[dict]:
    return [
        _page(),
        _entry("GET", BUNDLE, mime="application/javascript", body=APP_JS),
        _entry("GET", f"{SITE}/api/orders", body=json.dumps(ORDERS)),
        _entry("GET", f"{SITE}/api/orders/1002", body=json.dumps({"id": 1002, "total": 30})),
        _entry("POST", f"{SITE}/graphql", post=_gql("GetCart"),
               body=json.dumps({"data": {"getcart": {"id": "c1"}}})),
        _entry("POST", f"{SITE}/graphql", post=_gql("Me"),
               body=json.dumps({"data": {"me": {"id": "u1"}}})),
        _entry("POST", f"{SITE}/api/login",
               post=_json_post({"user": "ann", "password": "PW_SECRET"}), body='{"ok": true}'),
        _entry("POST", f"{SITE}/api/search", post=_form("token=TOK_SECRET&q=x"),
               body='{"hits": 0}'),
    ]


def _detached(entries: list[dict]) -> tuple[list[dict], dict[str, bytes]]:
    """ENTRIES with each body moved to a <sha1>.<ext> file, as Playwright's attach mode does."""
    entries = copy.deepcopy(entries)
    files: dict[str, bytes] = {}
    for entry in entries:
        post = entry["request"].get("postData")
        for body in (entry["response"]["content"], post):
            if body and body.get("text"):
                data = body.pop("text").encode()
                name = f"{hashlib.sha1(data).hexdigest()}.{EXTENSIONS.get(body['mimeType'], 'dat')}"
                files[name], body["_file"] = data, name
        if post:
            post["text"] = ""
    return entries, files


def _attach(entries: list[dict], folder: Path, name: str = "app.har") -> Path:
    attached, files = _detached(entries)
    folder.mkdir(parents=True, exist_ok=True)
    for file, data in files.items():
        (folder / file).write_bytes(data)
    return _write(folder, attached, name)


def _zip(path: Path, entries: list[dict], files: dict[str, bytes],
         compression: int = zipfile.ZIP_DEFLATED) -> Path:
    log = {"version": "1.2", "creator": {"name": "test", "version": "1"}, "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as archive:
        archive.writestr("har.har", json.dumps({"log": log}))
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _archive(entries: list[dict], path: Path) -> Path:
    return _zip(path, *_detached(entries))


def _file_of(entries: list[dict], path: str) -> str:
    entry = next(e for e in entries if urlsplit(e["request"]["url"]).path == path)
    return entry["response"]["content"]["_file"]


def _bodies(source: Path) -> list[tuple[str, str | None, str | None]]:
    return [(r.url, r.request_body, r.response_body) for r in load_traffic(source).requests]


def _attached_entry(name: str, path: str = "/api/orders") -> dict:
    entry = _entry("GET", f"{SITE}{path}")
    entry["response"]["content"]["_file"] = name
    return entry


@pytest.fixture
def stub_llm(monkeypatch) -> None:
    async def describe(endpoints):
        return [{**e, "description": f"stub: {e['method']} {e['url_template']}"} for e in endpoints]

    monkeypatch.setattr(pipeline, "describe_endpoints", describe)


@pytest.fixture
def sources(tmp_path) -> tuple[Path, Path, Path]:
    """The same traffic as an embedded HAR, an attach-mode HAR folder and a HAR archive."""
    return (_write(tmp_path, _traffic(), "embedded.har"),
            _attach(_traffic(), tmp_path / "hars").parent,
            _archive(_traffic(), tmp_path / "network.zip"))


def test_attached_and_archived_bodies_read_as_embedded_ones(sources):
    embedded, folder, archive = sources
    expected = load_traffic(embedded)

    assert "PW_SECRET" not in (folder / "app.har").read_text(encoding="utf-8")
    assert sum(body is not None for _, _, body in _bodies(embedded)) == 7
    for source in (folder, folder / "app.har", archive):
        assert _bodies(source) == _bodies(embedded), source
        assert load_traffic(source).js_bundles == expected.js_bundles == {BUNDLE: APP_JS}
    for command in ("baseline", "openapi"):
        outputs = [_invoke(command, str(source)) for source in sources]
        assert all(result.exit_code == 0 for result in outputs), command
        assert len({result.stdout for result in outputs}) == 1, command


def test_graphql_operations_are_told_apart_by_their_attached_bodies(sources):
    _, folder, _ = sources

    document = json.loads(_invoke("openapi", str(folder)).stdout)

    post = document["paths"]["/graphql"]["post"]
    assert post["x-rebrowse-graphql"] == ["GraphQL query: GetCart", "GraphQL query: Me"]
    assert post["x-rebrowse-effect"] == "read"
    graphql = [(route.labels, len(route.recordings))
               for route in build_routes(load_traffic(folder)) if route.display == "/graphql"]
    assert graphql == [(["GraphQL query: GetCart"], 1), (["GraphQL query: Me"], 1)]


def test_mock_answers_with_attached_payloads(sources):
    _, folder, archive = sources

    for source in (folder, archive):
        routes = build_routes(load_traffic(source))
        assert routes and not any(rec.missing_body for r in routes for rec in r.recordings)
    with _serving(load_traffic(archive)) as client:
        response = client.get("/api/orders")
    assert (response.status_code, response.json()) == (200, ORDERS)


def test_secrets_in_attached_bodies_are_redacted(sources):
    _, folder, archive = sources
    files = "".join(path.read_text(encoding="utf-8") for path in folder.iterdir())
    assert "PW_SECRET" in files and "TOK_SECRET" in files

    for source in (folder, archive):
        posted = {urlsplit(r.url).path: r.request_body for r in load_traffic(source).requests}
        assert json.loads(posted["/api/login"]) == {"user": "ann", "password": REDACTED}
        assert posted["/api/search"] == "token=%3Credacted%3E&q=x"
        for command in ("baseline", "openapi"):
            result = _invoke(command, str(source))
            assert result.exit_code == 0, result.output
            assert "PW_SECRET" not in result.stdout and "TOK_SECRET" not in result.stdout


def test_an_attachment_wins_over_embedded_text_as_in_playwright(tmp_path):
    orders = _entry("GET", f"{SITE}/api/orders", body='{"stale": true}')
    orders["response"]["content"]["_file"] = "orders.json"
    post = {**_json_post({"q": "stale"}), "_file": "search.dat"}
    search = _entry("POST", f"{SITE}/api/search", post=post, body='{"hits": 0}')
    (tmp_path / "orders.json").write_text(json.dumps(ORDERS), encoding="utf-8")
    (tmp_path / "search.dat").write_text('{"q": "x"}', encoding="utf-8")

    capture = load_har(_write(tmp_path, [_page(), orders, search]))

    assert [(r.request_body, r.response_body) for r in capture.requests[1:]] == [
        (None, json.dumps(ORDERS)), ('{"q": "x"}', '{"hits": 0}')]


def test_a_missing_attachment_is_an_input_error(tmp_path, fixture_site, hits):
    attached, files = _detached(_traffic())
    name = _file_of(attached, "/api/orders")
    har = _attach(_traffic(), tmp_path / "hars")
    (har.parent / name).unlink()
    embedded = _write(tmp_path, _traffic(), "embedded.har")
    out = tmp_path / "out.json"

    written = _invoke("baseline", str(har.parent), "-o", str(out))

    assert written.exit_code == 1 and not out.exists()
    assert json.loads(written.stdout)["error"] == (
        f"{har}: body attachment {name} is missing{MISSING}")
    assert _invoke("diff", str(har.parent), str(embedded)).exit_code == 2
    assert _invoke("contract", str(har), "--against", fixture_site).exit_code == 2
    del files[name]
    archive = _zip(tmp_path / "network.zip", attached, files)
    for args, code in ((("baseline", str(archive)), 1), (("diff", str(archive), str(embedded)), 2)):
        result = _invoke(*args)
        assert result.exit_code == code, result.output
        assert json.loads(result.stdout)["error"] == (
            f"{archive}: body attachment {name} is missing from the archive")
    assert not hits


def test_a_missing_attachment_rebrowse_never_reads_is_fine(tmp_path):
    image = _entry("GET", f"{SITE}/logo.png", mime="image/png")
    image["response"]["content"]["_file"] = f"{'0' * 40}.png"
    folder = tmp_path / "hars"
    _attach([_page(), image, _entry("GET", f"{SITE}/api/orders", body="{}")], folder)

    assert [r.response_body for r in load_traffic(folder).requests] == ["<html></html>", None, "{}"]


@pytest.mark.parametrize("name", ["../outside.json", "sub/x.json", "ABSOLUTE", ".hidden.json",
                                  "C:x.json", "sub\\x.json"])
def test_an_attachment_name_outside_the_hars_folder_is_refused(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "hars"
    (folder / "sub").mkdir(parents=True)
    if name == "ABSOLUTE":
        name = str(tmp_path / "outside.json")
    target = (folder / name).resolve()
    if target.is_relative_to(tmp_path.resolve()):
        target.write_text(json.dumps({"leak": "OUTSIDE_SECRET"}), encoding="utf-8")
    har = _write(folder, [_page(), _attached_entry(name)], "app.har")

    for args in (("baseline", str(folder)), ("openapi", str(har)), ("mock", str(har))):
        result = _invoke(*args)
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"] == f"{har}: attachment name {name!r} {UNSAFE}"
        assert "OUTSIDE_SECRET" not in result.stdout


def test_an_archive_entry_outside_its_root_is_refused(tmp_path):
    archive = _zip(tmp_path / "network.zip", [_page(), _attached_entry("sub/x.json")],
                   {"sub/x.json": b'{"leak": "OUTSIDE_SECRET"}'})

    with pytest.raises(HarError, match=f"attachment name 'sub/x.json' {UNSAFE}"):
        load_har(archive)


def test_a_symlinked_attachment_is_refused(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"leak": "OUTSIDE_SECRET"}), encoding="utf-8")
    folder = tmp_path / "hars"
    folder.mkdir()
    try:
        (folder / "linked.json").symlink_to(outside)
    except OSError as e:
        pytest.skip(f"cannot create a file symlink here: {e}")
    har = _write(folder, [_page(), _attached_entry("linked.json")], "app.har")

    result = _invoke("openapi", str(har))

    assert json.loads(result.stdout)["error"] == f"{har}: attachment name 'linked.json' {UNSAFE}"
    assert "OUTSIDE_SECRET" not in result.stdout


def test_a_bundle_shared_by_several_hars_is_read_from_the_first(tmp_path):
    suite = tmp_path / "suite"
    _attach(_traffic(), suite / "a")
    second = _attach(_traffic(), suite / "b")
    (second.parent / _file_of(_detached(_traffic())[0], "/static/app.js")).unlink()

    assert load_traffic(suite, every_script=True).js_bundles == {BUNDLE: APP_JS}


def test_an_attachment_over_the_body_limit_counts_as_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 64)
    entries = [_page(), _entry("GET", f"{SITE}/api/big", body=json.dumps({"x": "y" * 100})),
               _entry("GET", f"{SITE}/api/small", body='{"ok": true}')]
    folder = _attach(entries, tmp_path / "hars").parent

    capture = load_traffic(folder)

    assert [r.response_body for r in capture.requests[1:]] == [None, '{"ok": true}']
    routes = {route.display: route for route in build_routes(capture)}
    assert routes["/api/big"].recordings[0].missing_body
    assert not routes["/api/small"].recordings[0].missing_body


def test_an_attached_bundle_over_the_cap_is_read_only_by_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BUNDLE_CHARS", 32)
    har = _attach(_traffic(), tmp_path / "hars")

    assert load_har(har).js_bundles == {}
    report = json.loads(_invoke("coverage", str(har.parent)).stdout)
    assert report["bundles"] == 1
    assert [row["path"] for row in report["unrecorded"]] == ["/api/admin"]


def test_coverage_reads_attached_and_archived_bundles(sources):
    embedded, folder, archive = sources
    expected = json.loads(_invoke("coverage", str(embedded)).stdout)
    expected.pop("source")

    assert [row["path"] for row in expected["covered"]] == ["/api/orders"]
    assert [row["path"] for row in expected["unrecorded"]] == ["/api/admin"]
    for source in (folder, archive):
        result = _invoke("coverage", str(source))
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report.pop("source") == str(source.resolve())
        assert report == expected


def test_an_archive_is_a_source_like_a_har_file(sources, stub_llm, monkeypatch):
    embedded, _, archive = sources

    learned = _invoke("import-har", str(archive))

    assert learned.exit_code == 0, learned.output
    result = json.loads(learned.stdout)
    assert result["source"] == str(archive.resolve())
    graphql = sorted((e["description"], e["effect"]) for e in result["endpoints"]
                     if e["url"].endswith("/graphql"))
    assert graphql == [("GraphQL query: GetCart", "read"), ("GraphQL query: Me", "read")]
    assert len(list_all_skills()) == 1
    clean = _invoke("diff", str(archive), str(embedded))
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["changes"] == []
    monkeypatch.chdir(archive.parent)
    missing = _invoke("openapi", "./missing.zip")
    assert json.loads(missing.stdout)["error"] == "No such file: ./missing.zip"


def _not_a_zip(path: Path) -> None:
    path.write_text("not a zip", encoding="utf-8")


def _trace(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("trace.trace", "{}")


def _truncated(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("har.har", '{"log": {"entries": [')


def _cut_short(path: Path) -> None:
    data = _archive(_orders(), path).read_bytes()
    path.write_bytes(data[:len(data) // 2])


def _empty(path: Path) -> None:
    path.write_bytes(b"")


def _central_directory(path: Path) -> tuple[bytearray, int]:
    _trace(path)
    data = bytearray(path.read_bytes())
    return data, data.index(b"PK\x01\x02")


def _needs_version_98(path: Path) -> None:
    data, header = _central_directory(path)
    data[header + 6] = 98
    path.write_bytes(data)


def _undecodable_name(path: Path) -> None:
    data, header = _central_directory(path)
    flags, = struct.unpack_from("<H", data, header + 8)
    struct.pack_into("<H", data, header + 8, flags | 0x800)
    data[header + 46] = 0xC9
    path.write_bytes(data)


def _break_lzma_entry(path: Path, name: str) -> None:
    with zipfile.ZipFile(path) as archive:
        offset = archive.getinfo(name).header_offset
    data = bytearray(path.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
    data[offset + 30 + name_length + extra_length + 4] = 0xFF
    path.write_bytes(data)


def _broken_lzma(path: Path) -> None:
    _zip(path, _orders(), {}, zipfile.ZIP_LZMA)
    _break_lzma_entry(path, "har.har")


@pytest.mark.parametrize("make, error", [
    (_not_a_zip, "{} is not a zip archive"),
    (_cut_short, "{} is a damaged zip archive"),
    (_empty, "{} is a damaged zip archive"),
    (_trace, "{} holds no har.har; rebrowse reads HAR archives written by Playwright's recordHar"),
    (_truncated, "Cannot read {}!har.har as JSON: "),
    (_needs_version_98, "Cannot read {}: zip file version 9.8"),
    (_undecodable_name, "Cannot read {}: 'utf-8' codec can't decode byte 0xc9"),
    (_broken_lzma, "Cannot read {}!har.har: "),
])
def test_a_broken_archive_is_an_input_error(tmp_path, stub_llm, make, error):
    archive = tmp_path / "bad.zip"
    make(archive)
    cases = {
        ("baseline", str(archive)): 1,
        ("import-har", str(archive)): 1,
        ("diff", str(archive), str(archive)): 2,
        ("coverage", str(archive)): 2,
    }

    for args, code in cases.items():
        result = _invoke(*args)
        assert result.exit_code == code, (args, result.output)
        assert json.loads(result.stdout)["error"].startswith(error.format(archive)), args
    assert list_all_skills() == []


def test_a_suite_reads_har_archives_and_skips_other_zips(tmp_path):
    suite = tmp_path / "suite"
    _archive(_orders(), suite / "a" / "network.zip")
    _attach(_checkout(), suite / "b", "network.har")
    for folder in ("c", "d", "e"):
        (suite / folder).mkdir()
    _trace(suite / "c" / "trace.zip")
    _not_a_zip(suite / "d" / "junk.zip")
    for name, data in _detached(_traffic())[1].items():
        (suite / "e" / name).write_bytes(data)
    for skipped in (".cache/network.zip", "node_modules/pkg/network.zip", "a/._network.zip"):
        _archive(_traffic(), suite / skipped)

    result = _invoke("baseline", str(suite))

    assert _relative(suite) == ["a/network.zip", "b/network.har"]
    assert result.exit_code == 0, result.output
    assert result.stderr == f"[baseline] read 2 HAR files under {suite}\n"
    one = _write(tmp_path, [*_orders(), *_checkout()], "one.har")
    assert result.stdout == _invoke("baseline", str(one)).stdout


@pytest.mark.parametrize("damage", [_cut_short, _empty])
def test_a_damaged_zip_in_a_suite_is_an_input_error(tmp_path, damage):
    suite = tmp_path / "suite"
    _archive(_orders(), suite / "a" / "network.zip")
    (suite / "b").mkdir()
    damage(suite / "b" / "network.zip")
    one = _write(tmp_path, _orders(), "one.har")
    out = tmp_path / "out.json"
    error = f"{suite / 'b' / 'network.zip'} is a damaged zip archive"

    written = _invoke("baseline", str(suite), "-o", str(out))

    assert written.exit_code == 1 and not out.exists()
    assert json.loads(written.stdout)["error"] == error
    compared = _invoke("diff", str(one), str(suite))
    assert compared.exit_code == 2, compared.output
    assert json.loads(compared.stdout)["error"] == error


@pytest.mark.parametrize("damage", [_needs_version_98, _undecodable_name])
def test_a_zip_zipfile_cannot_list_in_a_suite_is_skipped(tmp_path, damage):
    suite = tmp_path / "suite"
    _archive(_orders(), suite / "a" / "network.zip")
    (suite / "b").mkdir()
    damage(suite / "b" / "trace.zip")

    result = _invoke("baseline", str(suite))

    assert _relative(suite) == ["a/network.zip"]
    assert result.exit_code == 0, result.output
    assert result.stderr == f"[baseline] read 1 HAR file under {suite}\n"
    clean = _invoke("diff", str(suite), str(_write(tmp_path, _orders(), "one.har")))
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["changes"] == []


def test_a_missing_request_body_attachment_is_an_input_error(tmp_path):
    attached, _ = _detached(_traffic())
    login = next(e for e in attached if urlsplit(e["request"]["url"]).path == "/api/login")
    name = login["request"]["postData"]["_file"]
    har = _attach(_traffic(), tmp_path / "hars")
    (har.parent / name).unlink()

    with pytest.raises(HarError) as caught:
        load_traffic(har.parent)

    assert str(caught.value) == f"{har}: body attachment {name} is missing{MISSING}"


def test_empty_embedded_text_falls_back_to_the_attachment(tmp_path):
    folder = tmp_path / "hars"
    folder.mkdir()
    (folder / "orders.json").write_text(json.dumps(ORDERS), encoding="utf-8")
    orders = _entry("GET", f"{SITE}/api/orders", body="")
    orders["response"]["content"]["_file"] = "orders.json"

    capture = load_har(_write(folder, [_page(), orders], "app.har"))

    assert capture.requests[1].response_body == json.dumps(ORDERS)


def test_an_attachment_that_is_a_folder_is_refused(tmp_path):
    folder = tmp_path / "hars"
    (folder / "orders.json").mkdir(parents=True)
    har = _write(folder, [_page(), _attached_entry("orders.json")], "app.har")

    with pytest.raises(HarError) as caught:
        load_har(har)

    assert str(caught.value) == f"{har}: attachment name 'orders.json' {UNSAFE}"


def test_an_attachment_that_is_not_utf8_is_read_with_replacement(tmp_path):
    folder = tmp_path / "hars"
    folder.mkdir()
    (folder / "page.html").write_bytes(b"<p>caf\xe9</p>")
    page = _entry("GET", f"{SITE}/", mime="text/html")
    page["response"]["content"]["_file"] = "page.html"

    capture = load_har(_write(folder, [page], "app.har"))

    assert capture.requests[0].response_body == "<p>caf\ufffd</p>"


def test_archive_names_are_limited_to_255_characters(tmp_path):
    longest, too_long = "a" * 250 + ".json", "a" * 251 + ".json"
    archive = _zip(tmp_path / "network.zip", [_page(), _attached_entry(longest)],
                   {longest: json.dumps(ORDERS).encode()})
    refused = _zip(tmp_path / "refused.zip", [_page(), _attached_entry(too_long)],
                   {too_long: json.dumps(ORDERS).encode()})

    assert load_har(archive).requests[1].response_body == json.dumps(ORDERS)
    with pytest.raises(HarError, match=UNSAFE):
        load_har(refused)


def test_an_upper_case_zip_suffix_is_an_archive(tmp_path):
    embedded = _write(tmp_path, _traffic(), "embedded.har")
    archive = _archive(_traffic(), tmp_path / "suite" / "NETWORK.ZIP")

    assert _bodies(archive) == _bodies(embedded)
    assert _relative(archive.parent) == ["NETWORK.ZIP"]
    assert _invoke("baseline", str(archive)).stdout == _invoke("baseline", str(embedded)).stdout


def test_an_archived_har_with_a_byte_order_mark_is_read(tmp_path):
    archive = tmp_path / "network.zip"
    log = {"version": "1.2", "creator": {"name": "test", "version": "1"}, "entries": _orders()}
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("har.har", "\ufeff" + json.dumps({"log": log}))

    assert load_har(archive).requests[1].response_body == json.dumps(ORDERS)


def test_a_corrupt_body_entry_is_an_input_error_naming_the_archive(tmp_path):
    attached, files = _detached(_traffic())
    name = _file_of(attached, "/api/orders")
    archive = _zip(tmp_path / "network.zip", attached, files, zipfile.ZIP_STORED)
    data = archive.read_bytes()
    archive.write_bytes(data.replace(b'"total": 30}]}', b'"total": 31}]}', 1))

    result = _invoke("diff", str(archive), str(_write(tmp_path, _traffic(), "embedded.har")))

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"].startswith(
        f"{archive}: cannot read body attachment {name}: ")


def test_a_zip_with_har_har_in_a_subfolder_is_not_a_recording(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    with zipfile.ZipFile(suite / "network.zip", "w") as archive:
        archive.writestr("out/har.har", json.dumps({"log": {"entries": _orders()}}))

    result = _invoke("baseline", str(suite))

    assert json.loads(result.stdout)["error"] == f"No .har files under {suite}"


def test_a_broken_lzma_body_entry_is_an_input_error_naming_the_archive(tmp_path):
    attached, files = _detached(_traffic())
    name = _file_of(attached, "/api/orders")
    archive = _zip(tmp_path / "network.zip", attached, files, zipfile.ZIP_LZMA)
    _break_lzma_entry(archive, name)

    result = _invoke("coverage", str(archive))

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"].startswith(
        f"{archive}: cannot read body attachment {name}: ")


def _echo(request: dict) -> str:
    return json.dumps({"method": "POST", "received": request["text"],
                       "content_type": "application/json"})


def test_contract_replays_graphql_reads_from_attached_bodies(tmp_path, fixture_site, hits):
    queries = [_gql("GetCart"), _gql("Me")]
    entries = [
        _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{fixture_site}/api/items",
               body=json.dumps({"items": [{"id": 0}], "next": "cur"})),
        *(_entry("POST", f"{fixture_site}/graphql", post=query, body=_echo(query),
                 headers=[("content-type", "application/json")]) for query in queries),
        _entry("POST", f"{fixture_site}/api/notes", post=_json_post({"text": "hi"}),
               body='{"ok": true}'),
    ]
    embedded = _write(tmp_path, entries, "embedded.har")
    folder = _attach(entries, tmp_path / "hars").parent

    replayed = _invoke("contract", str(folder), "--against", fixture_site)
    sent = dict(hits)
    hits.clear()
    expected = _invoke("contract", str(embedded), "--against", fixture_site)

    assert replayed.exit_code == expected.exit_code == 0, replayed.output
    report, baseline = json.loads(replayed.stdout), json.loads(expected.stdout)
    report.pop("source")
    baseline.pop("source")
    assert report == baseline
    assert (report["replayed"], [row["reason"] for row in report["skipped"]]) == (3, ["write"])
    assert sent == dict(hits) == {("GET", "/api/items"): 1, ("POST", "/graphql"): 2}


def test_import_har_of_one_attached_har_file_learns_each_graphql_operation(tmp_path, stub_llm):
    har = _attach(_traffic(), tmp_path / "hars")

    learned = _invoke("import-har", str(har))

    assert learned.exit_code == 0, learned.output
    result = json.loads(learned.stdout)
    graphql = sorted((e["description"], e["effect"]) for e in result["endpoints"]
                     if e["url"].endswith("/graphql"))
    assert graphql == [("GraphQL query: GetCart", "read"), ("GraphQL query: Me", "read")]


def test_an_attached_har_named_by_a_relative_path_reads_its_folder(tmp_path, monkeypatch):
    embedded = _write(tmp_path, _traffic(), "embedded.har")
    folder = _attach(_traffic(), tmp_path / "hars").parent
    monkeypatch.chdir(folder)

    assert _bodies(Path("app.har")) == _bodies(embedded)
    assert _invoke("openapi", "app.har").stdout == _invoke("openapi", str(embedded)).stdout


def test_body_limit_is_the_same_for_attached_and_embedded_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 64)
    at_limit, over = json.dumps({"x": "y" * 55}), json.dumps({"x": "y" * 56})
    assert (len(at_limit), len(over)) == (64, 65)
    entries = [_page(), _entry("GET", f"{SITE}/api/at", body=at_limit),
               _entry("GET", f"{SITE}/api/over", body=over)]
    embedded = _write(tmp_path, entries, "embedded.har")
    folder = _attach(entries, tmp_path / "hars").parent

    assert [body for _, _, body in _bodies(folder)][1:] == [at_limit, None]
    assert _bodies(folder) == _bodies(embedded)


def test_bundle_cap_is_the_same_for_attached_and_embedded_bundles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BUNDLE_CHARS", len(APP_JS))
    under, at_cap = f"{SITE}/static/under.js", f"{SITE}/static/at.js"
    entries = [_page(), _entry("GET", under, mime="application/javascript", body=APP_JS[:-1]),
               _entry("GET", at_cap, mime="application/javascript", body=APP_JS)]
    embedded = _write(tmp_path, entries, "embedded.har")
    har = _attach(entries, tmp_path / "hars")

    assert load_har(har).js_bundles == load_har(embedded).js_bundles == {under: APP_JS[:-1]}
    assert load_traffic(har.parent, every_script=True).js_bundles == {
        under: APP_JS[:-1], at_cap: APP_JS}


def test_an_archive_never_reads_a_file_beside_it(tmp_path):
    attached, files = _detached(_traffic())
    name = _file_of(attached, "/api/orders")
    del files[name]
    archive = _zip(tmp_path / "network.zip", attached, files)
    (tmp_path / name).write_text(json.dumps({"leak": "OUTSIDE_SECRET"}), encoding="utf-8")

    result = _invoke("openapi", str(archive))

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == (
        f"{archive}: body attachment {name} is missing from the archive")
    assert "OUTSIDE_SECRET" not in result.stdout


def test_a_folder_named_like_a_zip_is_walked(tmp_path):
    suite = tmp_path / "suite"
    _attach(_orders(), suite / "run.zip")

    result = _invoke("baseline", str(suite))

    assert _relative(suite) == ["run.zip/app.har"]
    assert result.exit_code == 0, result.output
    assert result.stdout == _invoke("baseline", str(_write(tmp_path, _orders(), "one.har"))).stdout


def test_attachments_of_entries_rebrowse_skips_may_be_missing(tmp_path):
    aborted = _attached_entry(f"{'1' * 40}.json", "/api/aborted")
    aborted["response"]["status"] = 0
    font = _entry("GET", f"{SITE}/static/font.woff2", mime="font/woff2")
    font["response"]["content"]["_file"] = f"{'2' * 40}.woff2"
    folder = tmp_path / "hars"
    _attach([_page(), aborted, font, _entry("GET", f"{SITE}/api/orders", body="{}")], folder)

    capture = load_traffic(folder)

    assert [(urlsplit(r.url).path, r.response_body) for r in capture.requests] == [
        ("/", "<html></html>"), ("/static/font.woff2", None), ("/api/orders", "{}")]


def test_a_git_lfs_pointer_in_a_suite_is_an_error_not_a_skip(tmp_path):
    pointer = "version https://git-lfs.github.com/spec/v1" + chr(10) + "oid sha256:ab" + chr(10)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "network.zip").write_text(pointer, encoding="utf-8")
    (tmp_path / "b.har").write_text(pointer, encoding="utf-8")

    with pytest.raises(HarError, match="git lfs pull"):
        har_files(tmp_path / "a")
    with pytest.raises(HarError, match="git lfs pull"):
        load_traffic(tmp_path / "b.har")


def test_a_skill_named_like_a_zip_domain_is_still_exported(tmp_path):
    result = CliRunner().invoke(main, ["openapi", "shop.zip"])

    assert json.loads(result.stdout)["error"] == "No skill matching 'shop.zip'."
