from __future__ import annotations

import asyncio
import base64
import json
import random
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner
from test_contract import _running, _scripted
from test_drift import _get
from test_har import _entry, _json_post, _write
from test_mock import PERSISTED, SITE

from rebrowse import contract, drift
from rebrowse.baseline import baseline_bytes, make_baseline
from rebrowse.capture.store import load_traffic, save_capture
from rebrowse.cli import main
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.safety import FORM

UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
KEYS = ("name", "id", "a-b", "naïve", "$ref", "1001", "42", "ann@corp.test", UUID, "2026-10-06",
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "", "has space")
LEAVES = ("", "s", 0, 2, 1.5, 4.0, 1e20, -0.0, True, False, None)


def _cli(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(main, list(args))
    return result.exit_code, json.loads(result.stdout)


def _value(rnd: random.Random, depth: int) -> Any:
    if depth > 14 or rnd.random() < 0.3:
        return rnd.choice(LEAVES)
    if rnd.random() < 0.6:
        return {rnd.choice(KEYS): _value(rnd, depth + 1) for _ in range(rnd.randint(0, 4))}
    return [_value(rnd, depth + 1) for _ in range(rnd.randint(0, 4))]


def _mutate(rnd: random.Random, value: Any, depth: int = 0) -> Any:
    if rnd.random() < 0.05:
        return _value(rnd, depth)
    if isinstance(value, dict):
        out = {k: _mutate(rnd, v, depth + 1) for k, v in value.items() if rnd.random() > 0.05}
        if rnd.random() < 0.1:
            out[rnd.choice(KEYS)] = _value(rnd, depth + 1)
        return out
    if isinstance(value, list):
        return [_mutate(rnd, v, depth + 1) for v in value if rnd.random() > 0.05]
    return value


def _capture(rnd: random.Random, bodies: list[tuple[str, int, Any]]) -> CaptureResult:
    requests = [RawRequest(
        url=f"{SITE}{path}", method="GET", response_status=status,
        response_headers={} if status == 204 else {"content-type": "application/json"},
        response_body=None if status == 204 else rnd.choice(["", ")]}'\n", "﻿"]) + json.dumps(body),
    ) for path, status, body in bodies]
    rnd.shuffle(requests)
    return CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=requests)


@pytest.mark.parametrize("seed", range(150))
def test_diff_judges_random_recordings_and_their_baselines_alike(seed):
    rnd = random.Random(seed)
    base_bodies, head_bodies = [], []
    for path in ("/api/a", "/api/b/1001", "/api/b/42", "/api/c"):
        for _ in range(rnd.randint(1, 3)):
            body = _value(rnd, 0)
            base_bodies.append((path, rnd.choice([200, 200, 404, 204]), body))
            head_bodies.append((path, rnd.choice([200, 200, 500, 204]), _mutate(rnd, body)))
    base, head = _capture(rnd, base_bodies), _capture(rnd, head_bodies)
    base_line, head_line = make_baseline(base), make_baseline(head)

    expected = drift.compare(base, head)

    assert drift.compare(base_line, head) == expected
    assert drift.compare(base, head_line) == expected
    assert drift.compare(base_line, head_line) == expected
    assert drift.compare(base, base_line) == []
    assert baseline_bytes(make_baseline(base_line)) == baseline_bytes(base_line)


def _replays(capture: CaptureResult) -> tuple[list[tuple], list[dict]]:
    replays, skipped = contract.plan(capture, "http://127.0.0.1:9")
    sent = sorted((replay.route.name, replay.request.method, str(replay.request.url),
                   sorted(replay.request.headers.items()), replay.request.content)
                  for replay in replays)
    return sent, sorted(skipped, key=lambda row: row["route"])


def test_contract_plans_the_same_replays_and_skips_from_a_baseline(tmp_path):
    headers = [("Accept", "application/json"), ("X-Requested-With", "XMLHttpRequest"),
               ("Authorization", "Bearer SECRET"), ("Cookie", "sid=SECRET"),
               ("Origin", SITE), ("Referer", f"{SITE}/app"), ("If-None-Match", 'W/"1"'),
               ("Content-Type", "application/json")]
    user = {"operationName": "User", "query": "query User($id: ID!) { user(id: $id) { id } }",
            "variables": {"id": 1001, "password": "SECRET"}}
    delete = {"operationName": "DeleteNote", "variables": {"id": 1001},
              "extensions": json.loads(PERSISTED)}
    add = {"operationName": "AddNote", "query": "mutation AddNote { addNote { id } }",
           "variables": {"text": "SECRET"}}
    form_read = urlencode({"query": "query Me { me { id } }", "password": "SECRET"})
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{SITE}/api/items/1001?page=2&api_key=SECRET", headers=headers,
               body=json.dumps({"id": 1001, "tags": ["a"]})),
        _entry("GET", f"{SITE}/api/items/1002", headers=headers, body=json.dumps({"id": 1002})),
        _entry("GET", f"{SITE}/api/cached", status=304, mime=""),
        _entry("GET", f"{SITE}/api/events", mime="text/event-stream", body="data: SECRET\n\n"),
        _entry("DELETE", f"{SITE}/api/notes/1001", post=_json_post({"reason": "SECRET"}), body="{}"),
        _entry("PUT", f"{SITE}/api/profile", post=_json_post({"name": "SECRET"}), body="{}"),
        _entry("POST", f"{SITE}/graphql", headers=headers, post=_json_post(user),
               body='{"data": {"user": {"id": 1001}}}'),
        _entry("POST", f"{SITE}/graphql", post=_json_post(delete), body='{"data": {}}'),
        _entry("POST", f"{SITE}/graphql", post=_json_post(add), body='{"data": {}}'),
        _entry("POST", f"{SITE}/gql", headers=[("Content-Type", FORM)],
               post={"mimeType": FORM, "text": form_read}, body='{"data": {"me": {}}}'),
        _get("https://api.app.example.com/v1/me", {"id": 1}),
    ])
    out = tmp_path / "api-baseline.json"
    code, _ = _cli("baseline", str(har), "-o", str(out))

    raw_sent, raw_skipped = _replays(load_traffic(har))
    sent, skipped = _replays(load_traffic(out))

    assert code == 0 and b"SECRET" not in out.read_bytes()
    assert (sent, skipped) == (raw_sent, raw_skipped)
    assert {row["reason"] for row in skipped} == {
        "not modified", "stream", "destructive", "write", "other host"}
    assert len(sent) == 4


def test_accepted_breaks_settle_a_baseline_diff_as_they_settle_its_recording(tmp_path):
    base = _write(tmp_path, [_get("/api/users/1001", {"id": 1001, "email": "a@x", "age": 3}),
                             _get("/api/orders/7", {"total": 1.5})], "base.har")
    head = _write(tmp_path, [_get("/api/users/1001", {"id": 1001, "age": "3"}),
                             _get("/api/orders/7", {"total": "1.5"})], "head.har")
    out, accepted = tmp_path / "api-baseline.json", tmp_path / "diff-accepted.json"
    _cli("baseline", str(base), "-o", str(out))
    _cli("diff", str(base), str(head), "--accepted", str(accepted), "--update-accepted")

    raw_code, raw = _cli("diff", str(base), str(head), "--accepted", str(accepted))
    code, report = _cli("diff", str(out), str(head), "--accepted", str(accepted))

    assert raw_code == code == 0
    assert (report["breaking"], report["accepted"], report["stale"]) == (0, 3, [])
    assert report["changes"] == raw["changes"]


def test_rewriting_a_baseline_in_place_keeps_its_bytes(tmp_path):
    har = _write(tmp_path, [_get("/api/users/1001", {"id": 1001, "name": "ann"}),
                            _get("/api/users/42", {"id": 42})])
    out = tmp_path / "api-baseline.json"
    out.write_text("an older baseline", encoding="utf-8")

    _cli("baseline", str(har), "-o", str(out))
    first = out.read_bytes()
    code, summary = _cli("baseline", str(out), "-o", str(out))

    assert code == 0 and out.read_bytes() == first
    assert (summary["recorded"], summary["requests"]) == (2, 2)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "api-baseline.json", "rebrowse-data", "session.har"]


def test_a_saved_capture_of_another_site_is_an_error(tmp_path):
    path = save_capture(CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/api/me", method="GET", response_status=200,
                   response_headers={"content-type": "application/json"}, response_body="{}")]))
    out = tmp_path / "api-baseline.json"

    code, report = _cli("baseline", str(path), "--domain", "other.test", "-o", str(out))

    assert code == 1 and "not other.test" in report["error"]
    assert not out.exists()


def test_the_page_url_becomes_the_site_root(tmp_path):
    har = _write(tmp_path, [
        _entry("GET", "https://ann:SECRET@app.example.com/home?invite=SECRET#SECRET",
               mime="text/html", body="<html></html>"),
        _get("/api/items", {"items": []}),
    ])
    out = tmp_path / "api-baseline.json"

    _cli("baseline", str(har), "-o", str(out))

    assert load_traffic(out).final_url == f"{SITE}/"


def test_stdout_and_out_write_the_same_utf8_bytes(tmp_path):
    har = _write(tmp_path, [_get("/api/städte?q=Zürich", {"naïve": "é", "名前": "名"})])
    out = tmp_path / "api-baseline.json"

    _cli("baseline", str(har), "-o", str(out))
    piped = CliRunner().invoke(main, ["baseline", str(har)])

    data = out.read_bytes()
    assert piped.exit_code == 0 and piped.stdout_bytes == data
    assert "Zürich".encode() in data and b"\\u00" not in data
    [req] = load_traffic(out).requests
    assert json.loads(req.response_body) == {"naïve": "", "名前": ""}


def test_a_baseline_is_far_smaller_than_its_recording(tmp_path):
    bundle = "x" * 200_000
    rows = [{"id": i, "email": f"user{i}@corp.test", "total": i * 1.5} for i in range(500)]
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html>" + bundle + "</html>"),
        _entry("GET", f"{SITE}/static/app.js", mime="application/javascript", body=bundle),
        _get("/api/orders", {"rows": rows}),
    ])
    out = tmp_path / "api-baseline.json"

    _cli("baseline", str(har), "-o", str(out))

    assert out.stat().st_size < 1_000 < Path(har).stat().st_size


@pytest.mark.parametrize("seed", range(20))
def test_contract_judges_random_recordings_and_their_baselines_alike(tmp_path, seed):
    rnd = random.Random(seed)
    headers = [("Accept", "application/json"), ("Accept", "*/*"), ("Cookie", "sid=SECRET"),
               ("X-Requested-With", "XMLHttpRequest"), ("traceparent", f"00-{seed}-01")]
    entries = [_entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")]
    answers = {}
    for path in ("/api/a", "/api/b/1001", "/api/b/1002", "/api/c?x=1", "/api/c?x=2"):
        body = _value(rnd, 0)
        for _ in range(rnd.randint(1, 3)):
            status = rnd.choice([200, 200, 200, 401, 404, 500, 304])
            entries.append(_entry("GET", f"{SITE}{path}", status=status,
                                  mime="" if status == 304 else "application/json",
                                  body=None if status == 304 else json.dumps(_mutate(rnd, body)),
                                  headers=rnd.sample(headers, rnd.randint(0, 3))))
        answers[path] = (rnd.choice([200, 200, 401, 404, 500]), "application/json",
                         json.dumps(_mutate(rnd, body)).encode())
    entries.append(_entry("POST", f"{SITE}/api/notes", post=_json_post({"t": "SECRET"}), body="{}"))
    rnd.shuffle(entries)
    har = _write(tmp_path, entries)
    out = tmp_path / "api-baseline.json"
    _cli("baseline", str(har), "-o", str(out))

    with _running(_scripted(answers)) as origin:
        raw = asyncio.run(contract.run_contract(load_traffic(har), origin))
        line = asyncio.run(contract.run_contract(load_traffic(out), origin))

    for report in (raw, line):
        report["skipped"] = sorted(row["route"] for row in report["skipped"])
    assert line == raw


def test_baseline_opens_no_connection(tmp_path, monkeypatch):
    har = _write(tmp_path, [_get("/api/users/1001", {"id": 1001}),
                            _get("https://api.stripe.com/v1/charges", {"id": "ch_1"})])
    save_capture(load_traffic(har))

    def refuse(*args, **kwargs):
        raise AssertionError("baseline opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    for source in (str(har), "app.example.com"):
        result = CliRunner().invoke(main, ["baseline", source])
        assert result.exit_code == 0, result.output
        assert b'"url": "https://app.example.com/api/users/1001"' in result.stdout_bytes


def test_base64_bodies_from_a_proxy_export_become_placeholders(tmp_path):
    def encoded(path: str, payload: Any) -> dict:
        text = base64.b64encode(json.dumps(payload).encode()).decode()
        return _entry("GET", f"{SITE}{path}", body=text, encoding="base64")

    base = _write(tmp_path, [encoded("/api/me", {"email": "SECRET@corp.test", "n": 4})], "base.har")
    head = _write(tmp_path, [encoded("/api/me", {"email": None, "n": 4.5})], "head.har")
    out = tmp_path / "api-baseline.json"

    _cli("baseline", str(base), "-o", str(out))

    assert b"SECRET" not in out.read_bytes()
    assert load_traffic(out).requests[0].response_body == '{"email":"","n":0}'
    assert _cli("diff", str(out), str(head))[1]["changes"] == _cli(
        "diff", str(base), str(head))[1]["changes"] != []


@pytest.mark.parametrize("body,served", [
    ("while(1);" + json.dumps({"a": [1]}), 'while(1);{"a":[0]}'),
    ("for(;;);\n" + json.dumps([{"b": "x"}]), 'for(;;);\n[{"b":""}]'),
    ('"just text"', '""'),
    ("42", "0"),
    ("true", "false"),
    ("null", "null"),
    ("  [3, 1]  ", "[0]"),
    ("   ", None),
])
def test_guards_and_top_level_scalars_keep_their_form(body, served):
    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/api/a", method="GET", response_status=200, response_body=body,
                   response_headers={"content-type": "application/json"})])

    [req] = make_baseline(capture).requests

    assert req.response_body == served
    assert drift.compare(capture, make_baseline(capture)) == []


def test_write_bodies_of_every_method_hold_no_value_and_other_schemes_are_dropped():
    def write(method: str, url: str, body: str) -> RawRequest:
        return RawRequest(url=url, method=method, request_body=body, response_status=200,
                          request_headers={"Content-Type": "application/json"},
                          response_headers={"content-type": "application/json"},
                          response_body="{}")

    capture = CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        write("PUT", f"{SITE}/api/profile", json.dumps({"name": "SECRET", "age": 41})),
        write("PATCH", f"{SITE}/api/profile", json.dumps([{"op": "SECRET"}, {"op": "SECRET2"}])),
        write("DELETE", f"{SITE}/api/notes/1001", json.dumps({"reason": "SECRET"})),
        write("POST", "wss://app.example.com/api/socket", json.dumps({"m": "SECRET"})),
    ])

    requests = make_baseline(capture).requests

    assert [(req.method, req.request_body) for req in requests] == [
        ("DELETE", '{"reason":""}'), ("PATCH", '[{"op":""}]'), ("PUT", '{"age":0,"name":""}')]
    assert "SECRET" not in baseline_bytes(make_baseline(capture)).decode()
