from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from test_contract import ITEMS, RESULTS, SITE, _get, _page, _running, _scripted
from test_drift import _users
from test_har import _entry, _json_post, _write
from test_mock import SITE as USERS_SITE
from test_mock import _gql

from rebrowse.accepted import (
    apply_accepted,
    load_accepted,
    matches,
    update_accepted,
    write_accepted,
)
from rebrowse.cli import main

ROUTE = "GET /api/users/{users_id}"
SEARCH_TOTAL = {"kind": "field_removed", "route": "GET /api/search", "field": "$.total",
                "base": ["integer"], "head": None}
GONE_STATUS = {"kind": "status", "route": "GET /api/nope", "base": [200], "head": [404]}
STATUS = {"severity": "breaking", "kind": "status", "route": "GET /a", "base": [200],
          "head": [410]}
REMOVED = {"severity": "breaking", "kind": "field_removed", "route": "GET /a", "field": "$.x",
           "base": ["string"], "head": None}


def _cli(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def _diff(tmp_path: Path, base: list, head: list, *args: str) -> Result:
    base_har = _write(tmp_path, base, "base.har")
    head_har = _write(tmp_path, head, "head.har")
    return _cli("diff", str(base_har), str(head_har), *args)


def _file(tmp_path: Path, entries: list) -> Path:
    path = tmp_path / "accepted.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _contract_har(tmp_path: Path, *entries: dict) -> Path:
    return _write(tmp_path, [_page(), _get("/api/search", {**RESULTS, "total": 3}),
                             _get("/api/nope", {"ok": True}), *entries])


def _rebrowse(*args: str, data: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "REBROWSE_DATA_DIR": str(data), "PYTHONIOENCODING": "utf-8:replace"}
    return subprocess.run([sys.executable, "-m", "rebrowse", *args], capture_output=True,
                          env=env, cwd=Path(__file__).resolve().parents[1], timeout=60,
                          check=False)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_change_copied_from_the_report_is_accepted(tmp_path):
    base = _users({"id": 1, "email": "a@x.test"}, {"id": 2, "email": "b@x.test"})
    head = _users({"id": 1}, {"id": 2})
    plain = _diff(tmp_path, base, head)
    [change] = json.loads(plain.stdout)["changes"]
    file = _file(tmp_path, [{**change, "reason": "moved to /api/profile in #412"}])

    result = _diff(tmp_path, base, head, "--accepted", str(file))

    report = json.loads(result.stdout)
    assert plain.exit_code == 1 and list(json.loads(plain.stdout)) == [
        "base", "head", "breaking", "changes"]
    assert list(report) == [
        "base", "head", "breaking", "accepted", "stale", "unchecked", "changes"]
    assert (result.exit_code, report["breaking"], report["accepted"], report["stale"],
            report["unchecked"]) == (0, 0, 1, [], [])
    assert report["changes"] == [
        {**change, "severity": "accepted", "reason": "moved to /api/profile in #412"}]
    assert result.stderr == ""


def test_an_entry_without_a_field_accepts_every_change_of_its_kind_on_the_route(tmp_path):
    base = _users({"id": 1, "email": "a", "name": "n"}, {"id": 2, "email": "b", "name": "m"})
    head = _users({"id": "1"}, {"id": "2"})
    file = _file(tmp_path, [{"kind": "field_removed", "route": ROUTE}])

    result = _diff(tmp_path, base, head, "--accepted", str(file))

    report = json.loads(result.stdout)
    assert (result.exit_code, report["breaking"], report["accepted"]) == (1, 1, 2)
    assert [(c["severity"], c["kind"], c["field"]) for c in report["changes"]] == [
        ("accepted", "field_removed", "$.email"),
        ("breaking", "field_type", "$.id"),
        ("accepted", "field_removed", "$.name"),
    ]


def test_an_entry_with_types_accepts_only_those_types(tmp_path):
    entry = {"kind": "field_type", "route": ROUTE, "field": "$.v", "base": ["string"],
             "head": ["null", "string"]}
    file = _file(tmp_path, [entry])
    base = _users({"v": "a"}, {"v": "b"})

    nullable = _diff(tmp_path, base, _users({"v": "a"}, {"v": None}), "--accepted", str(file))
    retyped = _diff(tmp_path, base, _users({"v": 7}, {"v": 8}), "--accepted", str(file))

    assert (nullable.exit_code, json.loads(nullable.stdout)["accepted"]) == (0, 1)
    report = json.loads(retyped.stdout)
    assert (retyped.exit_code, report["breaking"], report["accepted"]) == (1, 1, 0)
    assert report["changes"][0]["head"] == ["integer"] and report["stale"] == [entry]


def test_entries_that_match_no_breaking_change_are_stale(tmp_path):
    entry = {"kind": "field_removed", "route": ROUTE, "field": "$.email"}
    file = _file(tmp_path, [entry])

    same = _diff(tmp_path, _users({"id": 1}), _users({"id": 1}), "--accepted", str(file))
    sampled = _diff(tmp_path, _users({"id": 1, "email": "a"}, {"id": 2}),
                    _users({"id": 1, "nick": "n"}, {"id": 2}), "--accepted", str(file))

    assert same.exit_code == sampled.exit_code == 0
    assert json.loads(same.stdout)["stale"] == json.loads(sampled.stdout)["stale"] == [entry]
    assert f"1 entry in {file} matched no breaking change" in same.stderr
    assert "--update-accepted" in sampled.stderr
    assert [(c["severity"], c["kind"]) for c in json.loads(sampled.stdout)["changes"]] == [
        ("info", "field_removed"), ("info", "field_added")]


def test_update_accepted_creates_the_file_from_the_run(tmp_path):
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})
    head = _users({"id": "1"}, {"id": "2"})
    file = tmp_path / "accepted.json"
    args = ("--accepted", str(file), "--update-accepted")

    first = _diff(tmp_path, base, head, *args)
    written = file.read_bytes()
    second = _diff(tmp_path, base, head, *args)
    checked = _diff(tmp_path, base, head, "--accepted", str(file))

    assert json.loads(written) == [
        {"kind": "field_removed", "route": ROUTE, "field": "$.email", "base": ["string"],
         "head": None},
        {"kind": "field_type", "route": ROUTE, "field": "$.id", "base": ["integer"],
         "head": ["string"]},
    ]
    assert written.endswith(b"]\n") and b"\r" not in written
    report = json.loads(first.stdout)
    assert (first.exit_code, report["breaking"], report["accepted"], report["stale"]) == (
        0, 0, 2, [])
    assert second.exit_code == checked.exit_code == 0
    assert file.read_bytes() == written


def test_update_accepted_keeps_matching_entries_and_drops_stale_ones(tmp_path):
    broad = {"kind": "field_removed", "route": ROUTE, "reason": "profile moved in #412"}
    gone = {"kind": "status", "route": "GET /api/gone", "base": [200], "head": [404]}
    file = _file(tmp_path, [broad, gone])
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})

    result = _diff(tmp_path, base, _users({"id": "1"}, {"id": "2"}), "--accepted", str(file),
                   "--update-accepted")

    assert json.loads(file.read_text(encoding="utf-8")) == [broad, {
        "kind": "field_type", "route": ROUTE, "field": "$.id", "base": ["integer"],
        "head": ["string"]}]
    report = json.loads(result.stdout)
    assert (result.exit_code, report["accepted"], report["stale"]) == (0, 2, [])
    assert report["changes"][0]["reason"] == "profile moved in #412"
    assert "reason" not in report["changes"][1] and result.stderr == ""


def test_update_accepted_is_idempotent():
    entries = [{"kind": "status", "route": "GET /a", "reason": "r"},
               {"kind": "field_type", "route": "GET /b"}]

    once = update_accepted([STATUS, REMOVED], entries)

    assert once == [entries[0], {key: REMOVED[key] for key in
                                 ("kind", "route", "field", "base", "head")}]
    assert update_accepted([STATUS, REMOVED], once) == once


@pytest.mark.parametrize("text, problem", [
    ("not json", "as JSON"),
    ("{}", "must hold a JSON array"),
    ("[1]", "[0]: not an object"),
    ('[{"kind": "field_type"}]', "[0]: route is missing"),
    ('[{"kind": "no_response", "route": "GET /api/items"}]', "[0]: kind must be one of"),
    ('[{"kind": "blocked", "route": "GET /api/items"}]', "[0]: kind must be one of"),
    ('[{"kind": "route_added", "route": "GET /api/items"}]', "[0]: kind must be one of"),
    (('[{"kind": "status", "route": "GET /a"}, '
      '{"kind": "field_removed", "route": "GET /a", "feild": "$.x"}]'), '[1]: unknown key "feild"'),
    ('[{"kind": "status", "route": "GET /a", "reason": 3}]', "[0]: reason must be a string"),
    (None, "Cannot read"),
])
def test_a_bad_accepted_file_exits_2_before_anything_is_sent(
        tmp_path, fixture_site, hits, text, problem):
    file = tmp_path / "accepted.json"
    if text is not None:
        file.write_text(text, encoding="utf-8")
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS)])

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file))

    error = json.loads(result.stdout)["error"]
    assert result.exit_code == 2
    assert str(file) in error and problem in error
    assert sum(hits.values()) == 0


@pytest.mark.parametrize("command", [
    ("diff", "base.har", "head.har"),
    ("contract", "session.har", "--against", "http://127.0.0.1:9"),
])
def test_update_accepted_needs_accepted(command):
    result = _cli(*command, "--update-accepted")

    assert result.exit_code == 2 and "--accepted" in result.stderr


def test_contract_accepts_reviewed_breaks_without_sending_more(tmp_path, fixture_site, hits):
    har = _contract_har(tmp_path)
    file = tmp_path / "accepted.json"
    sent = []

    for args in ((), ("--accepted", str(file), "--update-accepted"), ("--accepted", str(file))):
        hits.clear()
        sent.append((_cli("contract", str(har), "--against", fixture_site, *args), dict(hits)))
    (plain, plain_hits), (updated, update_hits), (checked, check_hits) = sent

    assert json.loads(file.read_text(encoding="utf-8")) == [GONE_STATUS, SEARCH_TOTAL]
    assert (plain.exit_code, updated.exit_code) == (1, 0)
    report = json.loads(checked.stdout)
    assert (checked.exit_code, report["breaking"], report["accepted"], report["stale"]) == (
        0, 0, 2, [])
    assert plain_hits == update_hits == check_hits == {
        ("GET", "/api/search"): 1, ("GET", "/api/nope"): 1}


def test_contract_leaves_the_file_alone_while_a_replay_fails(tmp_path, fixture_site):
    file = _file(tmp_path, [GONE_STATUS, SEARCH_TOTAL])
    before = file.read_bytes()
    har = _contract_har(tmp_path, _get("/blocked", {"ok": True}))

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file),
                  "--update-accepted")

    report = json.loads(result.stdout)
    assert file.read_bytes() == before
    assert f"[contract] not updating {file}: 1 route failed to replay or answered with a server error" in result.stderr
    assert (result.exit_code, report["breaking"], report["accepted"]) == (1, 1, 2)
    assert [c["kind"] for c in report["changes"] if c["severity"] == "breaking"] == ["blocked"]


def test_runs_that_exit_2_never_write_the_file(tmp_path):
    file = tmp_path / "accepted.json"
    base = _write(tmp_path, _users({"id": 1}), "base.har")
    broken = tmp_path / "broken.har"
    broken.write_text("not json", encoding="utf-8")
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS)])
    update = ("--accepted", str(file), "--update-accepted")

    diffed = _cli("diff", str(base), str(broken), *update)
    tested = _cli("contract", str(har), "--against", f"http://127.0.0.1:{_free_port()}",
                  *update)

    assert diffed.exit_code == tested.exit_code == 2
    assert not file.exists()


def test_the_accepted_file_holds_no_recorded_values(tmp_path):
    base = _users(*({"members": {"alice@example.com": {"role": "admin"}}, "key": "s3cr3t-value"}
                    for _ in range(2)))
    head = _users(*({"members": {"alice@example.com": {}}} for _ in range(2)))
    file = tmp_path / "accepted.json"

    result = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")

    text = file.read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert "alice@example.com" not in text and "s3cr3t-value" not in text
    assert [(entry["kind"], entry["field"]) for entry in json.loads(text)] == [
        ("field_removed", "$.key"), ("field_removed", "$.members.*.role")]


def test_apply_accepted_returns_new_changes_and_leaves_its_input_alone():
    info = {"severity": "info", "kind": "field_added", "route": "GET /a", "field": "$.y",
            "base": None, "head": ["string"]}
    changes = [STATUS, REMOVED, info]
    snapshot = copy.deepcopy(changes)
    entries = [{"kind": "status", "route": "GET /a", "reason": "r"},
               {"kind": "status", "route": "GET /a", "base": [200]}]

    marked, stale, unchecked = apply_accepted(changes, entries)

    assert changes == snapshot
    assert marked == [{**STATUS, "severity": "accepted", "reason": "r"}, REMOVED, info]
    assert all(new is not old for new, old in zip(marked, changes))
    assert stale == unchecked == []


@pytest.mark.parametrize("entry, change, expected", [
    ({"kind": "status", "route": "GET /a"}, STATUS, True),
    ({"kind": "status", "route": "GET /a", "base": [200], "head": [410]}, STATUS, True),
    ({"kind": "status", "route": "GET /a", "severity": "info"}, STATUS, True),
    ({"kind": "status", "route": "GET /a", "head": [500]}, STATUS, False),
    ({"kind": "status", "route": "GET /a", "head": 503}, STATUS, False),
    ({"kind": "status", "route": "GET /a", "field": "$"}, STATUS, False),
    ({"kind": "status", "route": "GET /b"}, STATUS, False),
    ({"kind": "status", "route": "GET /a"}, {**STATUS, "severity": "info"}, False),
    ({"kind": "field_removed", "route": "GET /a", "field": "$.x", "head": None}, REMOVED, True),
    ({"kind": "field_removed", "route": "GET /a", "field": "$.y"}, REMOVED, False),
    ({"kind": "field_removed", "route": "GET /a", "base": ["integer"]}, REMOVED, False),
])
def test_matches_compares_only_what_the_entry_gives(entry, change, expected):
    assert matches(entry, change) is expected


def test_a_missing_file_is_empty_only_when_allowed(tmp_path):
    missing = tmp_path / "accepted.json"

    assert load_accepted(missing, missing_ok=True) == []
    with pytest.raises(ValueError, match="accepted.json"):
        load_accepted(missing)


def test_a_file_that_cannot_be_written_is_an_input_error(tmp_path):
    file = tmp_path / "no-such-dir" / "accepted.json"

    result = _diff(tmp_path, _users({"id": 1}), _users({"id": "1"}), "--accepted", str(file),
                   "--update-accepted")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"].startswith(f"Could not write {file}")


def test_a_challenge_page_is_never_accepted(tmp_path, fixture_site):
    entry = {"kind": "status", "route": "GET /blocked"}
    file = _file(tmp_path, [entry])
    har = _write(tmp_path, [_page(), _get("/blocked", {"ok": True})])

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file))

    report = json.loads(result.stdout)
    assert (result.exit_code, report["breaking"], report["accepted"]) == (1, 1, 0)
    assert [(c["severity"], c["kind"]) for c in report["changes"]] == [("breaking", "blocked")]
    assert (report["stale"], report["unchecked"]) == ([], [entry])


def test_route_level_changes_are_written_without_a_field(tmp_path):
    base = [_get("/api/save", {"id": 1}), _get("/api/stats", {"n": 1})]
    head = [_entry("GET", f"{SITE}/api/save", status=204, mime=""),
            _get("/api/stats", {"error": "gone"}, status=410)]
    file = tmp_path / "accepted.json"

    updated = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")
    checked = _diff(tmp_path, base, head, "--accepted", str(file))

    assert json.loads(file.read_text(encoding="utf-8")) == [
        {"kind": "body_empty", "route": "GET /api/save", "base": ["application/json"],
         "head": []},
        {"kind": "status", "route": "GET /api/stats", "base": [200], "head": [410]},
    ]
    report = json.loads(checked.stdout)
    assert updated.exit_code == checked.exit_code == 0
    assert (report["breaking"], report["accepted"], report["stale"]) == (0, 2, [])


def test_a_bom_and_crlf_file_is_read_and_rewritten_plain(tmp_path):
    entry = {"kind": "field_removed", "route": ROUTE, "reason": "r"}
    file = tmp_path / "accepted.json"
    file.write_bytes(b"\xef\xbb\xbf" + json.dumps([entry], indent=2).replace("\n", "\r\n").encode())
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})

    result = _diff(tmp_path, base, _users({"id": 1}, {"id": 2}), "--accepted", str(file),
                   "--update-accepted")

    assert (result.exit_code, json.loads(result.stdout)["accepted"]) == (0, 1)
    assert file.read_bytes() == (json.dumps([entry], indent=2) + "\n").encode()


def test_non_ascii_field_names_are_written_as_utf8_and_match_again(tmp_path):
    base = _users({"名前": "a"}, {"名前": "b"})
    head = _users({}, {})
    file = tmp_path / "accepted.json"

    updated = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")
    checked = _diff(tmp_path, base, head, "--accepted", str(file))

    assert '"$[\\"名前\\"]"'.encode() in file.read_bytes()
    assert updated.exit_code == checked.exit_code == 0
    assert json.loads(checked.stdout)["accepted"] == 1


def test_update_accepted_empties_a_file_that_only_holds_stale_entries(tmp_path):
    file = _file(tmp_path, [{"kind": "status", "route": "GET /api/gone"}])

    result = _diff(tmp_path, _users({"id": 1}), _users({"id": 1}), "--accepted", str(file),
                   "--update-accepted")

    assert (result.exit_code, json.loads(result.stdout)["stale"]) == (0, [])
    assert file.read_bytes() == b"[]\n" and result.stderr == ""


def test_an_unreachable_origin_leaves_an_existing_file_alone(tmp_path):
    file = _file(tmp_path, [GONE_STATUS])
    before = file.read_bytes()
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS)])

    result = _cli("contract", str(har), "--against", f"http://127.0.0.1:{_free_port()}",
                  "--accepted", str(file), "--update-accepted")

    assert result.exit_code == 2 and file.read_bytes() == before


def test_a_lone_surrogate_is_written_as_its_escape_and_read_back(tmp_path):
    file = _file(tmp_path, [{"kind": "status", "route": "GET /api/orders", "reason": "r"}])
    entries = [{"kind": "status", "route": "GET /api/x\ud800y", "reason": "für #412"}]

    write_accepted(file, entries)

    assert file.read_bytes() == (
        '[\n  {\n    "kind": "status",\n    "route": "GET /api/x\\ud800y",\n'
        '    "reason": "für #412"\n  }\n]\n').encode()
    assert load_accepted(file) == entries


def test_a_garbled_route_is_accepted_without_losing_the_reviewed_file(tmp_path, isolated):
    garbled = "/api/x\ud800y/list"
    base = [_get("/api/orders", {"id": 1, "total": 3}), _get(garbled, {"n": 1})]
    head = [_get("/api/orders", {"id": 1}), _get(garbled, {"n": "1"})]
    reviewed = {"kind": "field_removed", "route": "GET /api/orders", "reason": "totalCents"}
    file = _file(tmp_path, [reviewed])
    base_har, head_har = _write(tmp_path, base, "base.har"), _write(tmp_path, head, "head.har")
    args = ("diff", str(base_har), str(head_har), "--accepted", str(file))

    updated = _rebrowse(*args, "--update-accepted", data=isolated)
    checked = _rebrowse(*args, data=isolated)

    assert (updated.returncode, checked.returncode) == (0, 0), updated.stderr
    assert load_accepted(file) == [reviewed, {
        "kind": "field_type", "route": f"GET {garbled}", "field": "$.n", "base": ["integer"],
        "head": ["string"]}]
    assert json.loads(checked.stdout.decode("utf-8"))["accepted"] == 2


@pytest.mark.parametrize("text", [
    "[{",
    '[{"kind": "field_removed", "route": "GET /api/users/{users_id}", "feild": "$.email"}]',
])
def test_update_accepted_never_overwrites_an_invalid_file(tmp_path, text):
    file = tmp_path / "accepted.json"
    file.write_text(text, encoding="utf-8")
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})

    result = _diff(tmp_path, base, _users({"id": 1}, {"id": 2}), "--accepted", str(file),
                   "--update-accepted")

    assert result.exit_code == 2 and str(file) in json.loads(result.stdout)["error"]
    assert file.read_text(encoding="utf-8") == text


def test_graphql_operations_on_one_url_are_accepted_separately(tmp_path):
    def call(name: str, user: dict) -> dict:
        return _entry("POST", f"{SITE}/graphql", post=_gql(name),
                      body=json.dumps({"data": {"user": user}}))

    base = [call("GetUser", {"id": 1, "name": "ann"}), call("Feed", {"id": 1, "name": "ann"})]
    head = [call("GetUser", {"id": 1}), call("Feed", {"id": 1})]
    get_user = {"kind": "field_removed", "route": "POST /graphql [GraphQL query: GetUser]",
                "field": "$.data.user.name"}
    file = _file(tmp_path, [get_user])

    partial = _diff(tmp_path, base, head, "--accepted", str(file))
    updated = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")
    checked = _diff(tmp_path, base, head, "--accepted", str(file))

    report = json.loads(partial.stdout)
    assert (partial.exit_code, report["breaking"], report["accepted"]) == (1, 1, 1)
    assert [(c["severity"], c["route"]) for c in report["changes"]] == [
        ("breaking", "POST /graphql [GraphQL query: Feed]"),
        ("accepted", "POST /graphql [GraphQL query: GetUser]"),
    ]
    assert json.loads(file.read_text(encoding="utf-8")) == [get_user, {
        "kind": "field_removed", "route": "POST /graphql [GraphQL query: Feed]",
        "field": "$.data.user.name", "base": ["string"], "head": None}]
    assert updated.exit_code == checked.exit_code == 0
    assert json.loads(checked.stdout)["accepted"] == 2


def test_field_optional_and_content_type_breaks_round_trip(tmp_path):
    profile = f"{USERS_SITE}/api/profile"
    base = [*_users({"id": 1, "email": "a"}, {"id": 2, "email": "b"}),
            _entry("GET", profile, body=json.dumps({"name": "ann"}))]
    head = [*_users({"id": 1, "email": "a"}, {"id": 2}),
            _entry("GET", profile, mime="text/html", body="<form>sign in</form>")]
    file = tmp_path / "accepted.json"

    updated = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")
    checked = _diff(tmp_path, base, head, "--accepted", str(file))

    assert json.loads(file.read_text(encoding="utf-8")) == [
        {"kind": "content_type", "route": "GET /api/profile", "base": ["application/json"],
         "head": ["text/html"]},
        {"kind": "field_optional", "route": ROUTE, "field": "$.email", "base": ["string"],
         "head": ["string"]},
    ]
    report = json.loads(checked.stdout)
    assert updated.exit_code == checked.exit_code == 0
    assert (report["breaking"], report["accepted"], report["stale"]) == (0, 2, [])


def test_a_change_copied_from_an_accepted_report_still_matches(tmp_path):
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})
    head = _users({"id": 1}, {"id": 2})
    first = _file(tmp_path, [{"kind": "field_removed", "route": ROUTE, "reason": "#412"}])
    [accepted] = json.loads(_diff(tmp_path, base, head, "--accepted", str(first)).stdout)[
        "changes"]
    copied = tmp_path / "copied.json"
    copied.write_text(json.dumps([accepted]), encoding="utf-8")

    result = _diff(tmp_path, base, head, "--accepted", str(copied))

    report = json.loads(result.stdout)
    assert accepted["severity"] == "accepted"
    assert (result.exit_code, report["accepted"], report["stale"]) == (0, 1, [])
    assert report["changes"] == [accepted]


def test_contract_reports_stale_entries_under_its_own_name(tmp_path, fixture_site):
    entry = {"kind": "status", "route": "GET /api/items", "base": [200], "head": [404]}
    file = _file(tmp_path, [entry])
    har = _write(tmp_path, [_page(), _get("/api/items", ITEMS)])

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file))

    report = json.loads(result.stdout)
    assert (result.exit_code, report["breaking"], report["accepted"]) == (0, 0, 0)
    assert (report["stale"], report["unchecked"]) == ([entry], [])
    assert list(report)[-5:] == ["breaking", "accepted", "stale", "unchecked", "changes"]
    assert result.stderr.startswith(f"[contract] 1 entry in {file} matched no breaking change")


def test_a_failed_replay_does_not_create_a_missing_file(tmp_path, fixture_site):
    file = tmp_path / "accepted.json"
    har = _contract_har(tmp_path, _get("/blocked", {"ok": True}))

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file),
                  "--update-accepted")

    report = json.loads(result.stdout)
    assert not file.exists()
    assert f"not updating {file}: 1 route failed to replay or answered with a server error" in result.stderr
    assert (result.exit_code, report["accepted"]) == (1, 0)
    assert report["breaking"] == 3


def test_an_entry_for_a_failed_replay_is_unchecked_not_stale(tmp_path, fixture_site):
    blocked = {"kind": "status", "route": "GET /blocked"}
    file = _file(tmp_path, [blocked, SEARCH_TOTAL])
    before = file.read_bytes()
    har = _contract_har(tmp_path, _get("/blocked", {"ok": True}))

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file),
                  "--update-accepted")

    report = json.loads(result.stdout)
    assert file.read_bytes() == before
    assert result.stderr == f"[contract] not updating {file}: 1 route failed to replay or answered with a server error\n"
    assert (report["accepted"], report["stale"], report["unchecked"]) == (1, [], [blocked])


def test_a_refused_update_does_not_suggest_rerunning_it(tmp_path, fixture_site):
    compared = {"kind": "content_type", "route": "GET /api/search"}
    file = _file(tmp_path, [compared])
    har = _contract_har(tmp_path, _get("/blocked", {"ok": True}))

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file),
                  "--update-accepted")

    assert json.loads(result.stdout)["stale"] == [compared]
    assert result.stderr.splitlines() == [
        f"[contract] not updating {file}: 1 route failed to replay or answered with a server error",
        f"[contract] 1 entry in {file} matched no breaking change; remove stale entries"]


def test_contract_keeps_entries_for_routes_it_skips(tmp_path, fixture_site, hits):
    write = {"kind": "field_removed", "route": "POST /api/notes", "field": "$.id"}
    sibling = {"kind": "status", "route": "GET api.app.local/api/x", "reason": "moved"}
    gone = {"kind": "status", "route": "GET /api/gone"}
    file = _file(tmp_path, [write, sibling, gone])
    har = _contract_har(
        tmp_path,
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body='{"id": 1}'),
        _get("http://api.app.local/api/x", {"ok": True}))
    args = ("contract", str(har), "--against", fixture_site, "--accepted", str(file))

    plain = _cli(*args)
    updated = _cli(*args, "--update-accepted")
    checked = _cli(*args)

    report = json.loads(plain.stdout)
    assert (report["stale"], report["unchecked"]) == ([gone], [write, sibling])
    assert f"1 entry in {file} matched no breaking change" in plain.stderr
    assert updated.exit_code == checked.exit_code == 0
    assert json.loads(file.read_text(encoding="utf-8")) == [
        write, sibling, GONE_STATUS, SEARCH_TOTAL]
    report = json.loads(checked.stdout)
    assert (report["accepted"], report["stale"], report["unchecked"]) == (2, [], [write, sibling])
    assert checked.stderr == "" and hits[("POST", "/api/notes")] == 0


def test_diff_keeps_entries_for_routes_only_one_recording_has(tmp_path):
    orders = {"kind": "field_removed", "route": "GET /api/orders", "field": "$.total"}
    stats = {"kind": "status", "route": "GET /api/stats"}
    users = {"kind": "field_removed", "route": ROUTE}
    file = _file(tmp_path, [orders, stats, users])
    base = [*_users({"id": 1}), _get(f"{USERS_SITE}/api/orders", {"total": 3})]
    head = [*_users({"id": 1}), _get(f"{USERS_SITE}/api/stats", {"n": 1})]

    plain = _diff(tmp_path, base, head, "--accepted", str(file))
    updated = _diff(tmp_path, base, head, "--accepted", str(file), "--update-accepted")

    report = json.loads(plain.stdout)
    assert (plain.exit_code, report["stale"], report["unchecked"]) == (0, [users],
                                                                     [orders, stats])
    assert json.loads(file.read_text(encoding="utf-8")) == [orders, stats]
    report = json.loads(updated.stdout)
    assert (report["stale"], report["unchecked"], updated.stderr) == ([], [orders, stats], "")


def test_entries_on_uncompared_routes_still_accept_what_was_compared():
    other = {**REMOVED, "route": "GET /b"}
    entries = [{"kind": "status", "route": "GET /a"}, {"kind": "field_type", "route": "GET /a"},
               {"kind": "status", "route": "GET /c"}]

    marked, stale, unchecked = apply_accepted([STATUS, other], entries, {"GET /a"})

    assert [change["severity"] for change in marked] == ["accepted", "breaking"]
    assert (stale, unchecked) == ([entries[2]], [entries[1]])
    assert update_accepted([STATUS, other], entries, {"GET /a"}) == [
        *entries[:2], {key: other[key] for key in ("kind", "route", "field", "base", "head")}]


def test_failures_and_info_changes_are_never_matched_or_appended():
    blocked = {"severity": "breaking", "kind": "blocked", "route": "GET /a", "error": "e",
               "failed": 1}
    added = {"severity": "info", "kind": "route_added", "route": "GET /b", "base": None,
             "head": [200]}
    entries = [{"kind": "blocked", "route": "GET /a"}, {"kind": "route_added", "route": "GET /b"}]

    marked, stale, unchecked = apply_accepted([blocked, added], entries)

    assert not any(matches(entry, change) for entry in entries for change in (blocked, added))
    assert marked == [blocked, added] and stale == entries and unchecked == []
    assert update_accepted([blocked, added], []) == []


def test_every_entry_that_matches_is_used_and_the_first_one_gives_the_reason():
    entries = [{"kind": "status", "route": "GET /a"},
               {"kind": "status", "route": "GET /a", "head": [410], "reason": "later"}]

    marked, stale, unchecked = apply_accepted([STATUS], entries)

    assert marked == [{**STATUS, "severity": "accepted"}] and stale == unchecked == []
    assert update_accepted([STATUS], entries) == entries


def test_accepting_a_regression_never_hides_a_failure_on_the_same_route(tmp_path):
    challenge = b"<html><title>Just a moment...</title></html>"
    entry = {"kind": "status", "route": "GET /api/orders/{orders_id}", "reason": "#9"}
    file = _file(tmp_path, [entry])
    before = file.read_bytes()
    har = _write(tmp_path, [_page(), _get("/api/orders/101", {"id": 101}),
                            _get("/api/orders/102", {"id": 102})])
    with _running(_scripted({
        "/api/orders/101": (403, "text/html", challenge),
        "/api/orders/102": (404, "application/json", b'{"error": "gone"}'),
    })) as origin:
        result = _cli("contract", str(har), "--against", origin, "--accepted", str(file),
                      "--update-accepted")

    report = json.loads(result.stdout)
    assert (result.exit_code, report["breaking"], report["accepted"]) == (1, 1, 1)
    assert [(c["severity"], c["kind"]) for c in report["changes"]] == [
        ("accepted", "status"), ("breaking", "blocked")]
    assert (report["stale"], report["unchecked"]) == ([], [])
    assert file.read_bytes() == before and "not updating" in result.stderr


@pytest.mark.parametrize("command", [
    ("diff", "page.har", "page.har"),
    ("contract", "page.har", "--against", "http://127.0.0.1:9"),
])
def test_an_input_without_api_traffic_never_creates_the_file(tmp_path, command):
    _write(tmp_path, [_page()], "page.har")
    file = tmp_path / "accepted.json"
    args = [str(tmp_path / arg) if arg.endswith(".har") else arg for arg in command]

    result = _cli(*args, "--accepted", str(file), "--update-accepted")

    assert result.exit_code == 2 and "error" in json.loads(result.stdout)
    assert not file.exists()


def test_diff_checks_the_accepted_file_before_its_inputs(tmp_path):
    file = _file(tmp_path, [{"kind": "blocked", "route": "GET /a"}])

    result = _cli("diff", str(tmp_path / "missing.har"), str(tmp_path / "gone.har"),
                  "--accepted", str(file))

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"].startswith(f"{file}[0]: kind must be one of")


@pytest.mark.parametrize("entry, problem", [
    ({"route": "GET /a"}, "kind must be one of"),
    ({"kind": ["status"], "route": "GET /a"}, "kind must be one of"),
    ({"kind": "field_added", "route": "GET /a"}, "kind must be one of"),
    ({"kind": "route_missing", "route": "GET /a"}, "kind must be one of"),
    ({"kind": "status", "route": 3}, "route must be a string"),
    ({"kind": "field_type", "route": "GET /a", "field": None}, "field must be a string"),
    ({"kind": "status", "route": "GET /a", "error": "e"}, 'unknown key "error"'),
])
def test_an_invalid_entry_names_its_index(tmp_path, entry, problem):
    file = _file(tmp_path, [{"kind": "status", "route": "GET /a"}, entry])

    with pytest.raises(ValueError) as raised:
        load_accepted(file)

    assert str(raised.value).startswith(f"{file}[1]: {problem}")


def test_every_documented_key_loads_and_severity_is_ignored(tmp_path):
    entry = {"severity": {"any": "thing"}, "kind": "field_type", "route": ROUTE,
             "field": "$.v", "base": ["string"], "head": ["integer"], "reason": "#7"}
    file = _file(tmp_path, [entry])

    assert load_accepted(file) == [entry]
    assert matches(entry, {"severity": "breaking", "kind": "field_type", "route": ROUTE,
                           "field": "$.v", "base": ["string"], "head": ["integer"]})


def test_an_empty_file_keeps_the_exit_code_and_adds_the_keys(tmp_path):
    base = _users({"id": 1, "email": "a"}, {"id": 2, "email": "b"})
    head = _users({"id": 1}, {"id": 2})
    file = _file(tmp_path, [])

    plain = _diff(tmp_path, base, head)
    result = _diff(tmp_path, base, head, "--accepted", str(file))

    report = json.loads(result.stdout)
    assert result.exit_code == plain.exit_code == 1
    assert (report["breaking"], report["accepted"], report["stale"], report["unchecked"]) == (
        1, 0, [], [])
    assert report["changes"] == json.loads(plain.stdout)["changes"] and result.stderr == ""


def test_several_stale_entries_are_counted_in_one_warning(tmp_path):
    entries = [{"kind": "status", "route": ROUTE}, {"kind": "field_type", "route": ROUTE}]
    file = _file(tmp_path, entries)

    result = _diff(tmp_path, _users({"id": 1}), _users({"id": 1}), "--accepted", str(file))

    assert (result.exit_code, json.loads(result.stdout)["stale"]) == (0, entries)
    assert result.stderr == (f"[diff] 2 entries in {file} matched no breaking change; remove "
                             "stale entries or rerun with --update-accepted\n")


def test_an_accepted_path_that_is_a_directory_is_a_usage_error(tmp_path):
    result = _diff(tmp_path, _users({"id": 1}), _users({"id": "1"}), "--accepted",
                   str(tmp_path))

    assert result.exit_code == 2 and "--accepted" in result.stderr


@pytest.mark.parametrize("entry,problem", [
    ({"kind": "status", "route": "GET /a", "head": [200, 503]}, "server error"),
    ({"kind": "status", "route": "GET /a", "field": "$.x"}, "field does not apply"),
    ({"kind": "body_empty", "route": "GET /a", "field": "$"}, "field does not apply"),
])
def test_outages_and_route_level_fields_cannot_be_accepted(tmp_path, entry, problem):
    with pytest.raises(ValueError, match=problem):
        load_accepted(_file(tmp_path, [entry]))


def test_a_broad_status_entry_never_accepts_a_server_error():
    broad = {"kind": "status", "route": "GET /a"}
    outage = {**STATUS, "head": [200, 503]}

    changes, stale, _ = apply_accepted([outage, STATUS], [broad])

    assert [change["severity"] for change in changes] == ["breaking", "accepted"]
    assert stale == []
    assert update_accepted([outage], []) == []


def test_an_outage_stops_contract_from_rewriting_the_file(tmp_path, fixture_site):
    file = _file(tmp_path, [GONE_STATUS])
    before = file.read_bytes()
    har = _contract_har(tmp_path, _get("/api/fail", {"ok": True}))

    result = _cli("contract", str(har), "--against", fixture_site, "--accepted", str(file),
                  "--update-accepted")

    report = json.loads(result.stdout)
    assert result.exit_code == 1 and file.read_bytes() == before
    assert "not updating" in result.stderr
    assert [(c["route"], c["severity"]) for c in report["changes"]
            if c["kind"] == "status"] == [("GET /api/fail", "breaking"),
                                         ("GET /api/nope", "accepted")]


def test_base_and_head_match_as_json_not_as_python_values():
    entry = {"kind": "status", "route": "GET /a", "base": [200], "head": [True]}
    assert not matches(entry, {**STATUS, "head": [1]})
