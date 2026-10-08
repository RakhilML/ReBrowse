from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from test_baseline import ADD_NOTE, DOCUMENTS, JWT, PAIRS, TOKEN, UUID
from test_drift import _get, _users
from test_har import _entry, _json_post, _with_cookies, _write
from test_mock import HTTP_CLIENTS, SITE, _app, _gql
from test_openapi import assert_valid

from rebrowse import config, drift, openapi
from rebrowse.capture.store import save_capture
from rebrowse.cli import main
from rebrowse.llm import client as llm
from rebrowse.models import CaptureResult, EndpointDescriptor, RawRequest, SkillManifest
from rebrowse.safety import REDACTED
from rebrowse.store import skills as store
from rebrowse.store.skills import list_all_skills, save_skill

SIDES = {f"{name} {side}": entries for name, pair in PAIRS.items()
         for side, entries in zip(("base", "head"), pair)} | {"app": _app()}


def _invoke(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(main, list(args))
    return result.exit_code, json.loads(result.stdout)


def _document(source: Path, out: Path, *args: str) -> dict:
    code, summary = _invoke("openapi", str(source), "-o", str(out), *args)
    assert code == 0, summary
    return json.loads(out.read_text(encoding="utf-8"))


def _har_document(tmp_path: Path, entries: list, *args: str) -> dict:
    document = _document(_write(tmp_path, entries), tmp_path / "api.json", *args)
    assert_valid(document)
    return document


def _schema(document: dict, path: str, status: str = "200", method: str = "get") -> dict:
    response = document["paths"][path][method]["responses"][status]
    return response["content"]["application/json"]["schema"]


def _home() -> dict:
    return _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>")


def _skill(domain: str) -> SkillManifest:
    skill = SkillManifest(name=domain, domain=domain, endpoints=[
        EndpointDescriptor(url_template=f"https://{domain}/api/items")])
    save_skill(skill)
    return skill


@pytest.mark.parametrize("entries", SIDES.values(), ids=SIDES.keys())
def test_a_har_and_its_baseline_give_the_same_bytes(tmp_path, entries):
    har = _write(tmp_path, entries)
    from_har, baseline, from_baseline = (tmp_path / n for n in ("a.json", "b.json", "c.json"))

    _document(har, from_har)
    assert _invoke("baseline", str(har), "-o", str(baseline))[0] == 0
    _document(baseline, from_baseline)

    data = from_har.read_bytes()
    assert from_baseline.read_bytes() == data
    _document(har, from_har)
    assert from_har.read_bytes() == data
    assert_valid(json.loads(data))


def test_required_fields_are_the_ones_diff_breaks_on(tmp_path):
    base = _write(tmp_path, _users({"id": 1001, "email": "a@x.test", "avatar": "a.png"},
                                   {"id": 42, "email": "b@x.test"}), "base.har")
    head = _write(tmp_path, _users({"id": 1001}, {"id": 42}), "head.har")

    _, report = _invoke("diff", str(base), str(head))
    schema = _schema(_document(base, tmp_path / "api.json"), "/api/users/{users_id}")

    assert [(c["severity"], c["kind"], c["field"]) for c in report["changes"]] == [
        ("info", "field_removed", "$.avatar"), ("breaking", "field_removed", "$.email")]
    assert schema["required"] == ["email", "id"]
    assert schema["properties"]["avatar"] == {"type": "string"}


def test_map_keys_are_documented_as_additional_properties(tmp_path):
    document = _har_document(tmp_path, PAIRS["map keys"][0])

    members = _schema(document, "/api/team")["properties"]["members"]
    assert members == {"type": "object", "additionalProperties": {
        "type": "object", "properties": {"role": {"type": "string"}, "since": {"type": "integer"}},
        "required": ["role"]}}
    users = _schema(document, "/api/state")["properties"]["entities"]["properties"]["users"]
    assert users["additionalProperties"]["properties"] == {"name": {"type": "string"}}
    assert _schema(document, "/api/keys")["properties"]["k"] == {
        "type": "object", "additionalProperties": {"type": "integer"}}
    text = json.dumps(document, ensure_ascii=False)
    for value in ("ann@corp.test", "bob@corp.test", UUID, TOKEN, "2026-10-06"):
        assert value not in text, value


def test_each_recorded_status_gets_its_own_response(tmp_path):
    document = _har_document(tmp_path, [
        _home(),
        _get("/api/flaky", {"error": "boom"}, status=500), _get("/api/flaky", {"ok": True}),
        _entry("GET", f"{SITE}/api/session", status=302, mime="",
               response_headers=[("Location", f"{SITE}/login")]),
        _entry("GET", f"{SITE}/api/feed", status=304, mime=""),
        _entry("DELETE", f"{SITE}/api/notes/1001", status=204, mime=""),
        _entry("GET", f"{SITE}/app/dashboard", mime="text/html", body="<p>dashboard</p>"),
        _get("/api/odd", {"ok": True}, status=299),
        _get("/api/n", {"a": 1, "b": "x", "c": []}),
        _get("/api/n", {"a": 2.5, "b": None, "c": []}),
    ])
    paths = document["paths"]

    flaky = paths["/api/flaky"]["get"]["responses"]
    assert [(status, response["description"]) for status, response in flaky.items()] == [
        ("200", "OK"), ("500", "Internal Server Error")]
    assert _schema(document, "/api/flaky")["properties"] == {"ok": {"type": "boolean"}}
    assert _schema(document, "/api/flaky", "500")["properties"] == {"error": {"type": "string"}}
    assert paths["/api/session"]["get"]["responses"] == {"302": {"description": "Found"}}
    assert paths["/api/feed"]["get"]["responses"] == {"304": {"description": "Not Modified"}}
    assert paths["/api/notes/{notes_id}"]["delete"]["responses"] == {
        "204": {"description": "No Content"}}
    assert paths["/app/dashboard"]["get"]["responses"] == {
        "200": {"description": "OK", "content": {"text/html": {}}}}
    assert paths["/api/odd"]["get"]["responses"]["299"]["description"] == "Recorded response"
    assert _schema(document, "/api/n")["properties"] == {
        "a": {"type": "number"}, "b": {"type": ["null", "string"]}, "c": {"type": "array"}}


def test_browser_har_media_types_on_bodiless_responses_are_not_content(tmp_path):
    entries = [
        _home(),
        _entry("GET", f"{SITE}/api/old", status=302, mime="x-unknown",
               response_headers=[("Location", f"{SITE}/login")]),
        _entry("DELETE", f"{SITE}/api/notes/1001", status=204, mime="x-unknown"),
        _entry("GET", f"{SITE}/api/feed", status=304, mime="application/json"),
        _entry("GET", f"{SITE}/api/stats", status=304, mime="application/json",
               response_headers=[("Content-Type", "application/json")]),
        _entry("GET", f"{SITE}/api/reset", status=205,
               response_headers=[("Content-Type", "application/json")]),
        _get("/api/stats", {"hits": 3}),
    ]
    document = _har_document(tmp_path, entries)
    paths = document["paths"]

    assert paths["/api/old"]["get"]["responses"] == {"302": {"description": "Found"}}
    assert paths["/api/notes/{notes_id}"]["delete"]["responses"] == {
        "204": {"description": "No Content"}}
    assert paths["/api/feed"]["get"]["responses"] == {"304": {"description": "Not Modified"}}
    assert paths["/api/reset"]["get"]["responses"] == {"205": {"description": "Reset Content"}}
    stats = paths["/api/stats"]["get"]["responses"]
    assert stats["304"] == {"description": "Not Modified"}
    assert list(stats["200"]["content"]) == ["application/json"]
    assert "x-unknown" not in json.dumps(document)


def test_graphql_operations_at_one_url_merge_into_one_operation(tmp_path):
    document = _har_document(tmp_path, PAIRS["graphql"][0])
    post, get = (document["paths"]["/graphql"][method] for method in ("post", "get"))
    labels = ["GraphQL mutation: AddNote", "GraphQL query: Feed", "GraphQL query: GetUser"]

    assert (post["x-rebrowse-graphql"], post["x-rebrowse-effect"]) == (labels, "write")
    media = post["requestBody"]["content"]["application/json"]
    assert list(media["examples"]) == labels and "example" not in media
    data = _schema(document, "/graphql", method="post")["properties"]["data"]
    assert sorted(data["properties"]) == ["addNote", "feed", "user"] and "required" not in data
    assert [p["name"] for p in get["parameters"]] == ["extensions", "operationName"]
    assert "requestBody" not in get and get["x-rebrowse-effect"] == "read"


def test_parameters_are_documented_from_every_recorded_request(tmp_path):
    document = _har_document(tmp_path, [
        _home(),
        _get("/api/orders/1001?status=open&page=2&_=1700000000000", {"id": 1}, headers=[
            ("X-Tenant", "acme"), ("Cookie", "sid=COOKIE"), ("Authorization", f"Bearer {JWT}")]),
        _get(f"/api/orders/42?status=closed&access_token={JWT}", {"id": 2},
             headers=[("X-Tenant", "acme")]),
    ])
    params = document["paths"]["/api/orders/{orders_id}"]["get"]["parameters"]

    assert [(p["in"], p["name"], p["required"], p["example"]) for p in params] == [
        ("path", "orders_id", True, "1001"),
        ("query", "access_token", False, REDACTED),
        ("query", "page", False, "2"),
        ("query", "status", True, "open"),
        ("header", "x-tenant", True, "acme"),
    ]
    text = json.dumps(document).lower()
    for value in ("cookie", "authorization", JWT.lower()):
        assert value not in text, value


def test_no_credential_or_recorded_value_reaches_the_document(tmp_path):
    profile = {"email": "SENTINEL@corp.test", "owners": {"ann@corp.test": {"name": "SENTINEL"}}}
    document = _har_document(tmp_path, [
        _home(),
        _with_cookies(_get("/api/me", profile, headers=[
            ("Cookie", "sid=SENTINEL-COOKIE"), ("Authorization", "Bearer SENTINEL-BEARER")])),
        *(_entry("POST", f"{SITE}/graphql", post=_json_post(body),
                 body=json.dumps({"data": {"ok": "SENTINEL"}})) for body in DOCUMENTS),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ADD_NOTE),
               body='{"data": {"addNote": {"id": "SENTINEL"}}}'),
        _get(f"/api/reset/{JWT}", {"ok": "SENTINEL"}),
    ])
    text = json.dumps(document, ensure_ascii=False)

    for value in ("SENTINEL", "SENTINEL-PW", "HUNTER2", "COOKIE_SECRET", JWT):
        assert value not in text, value
    assert not any(JWT in path for path in document["paths"])
    assert len(document["paths"]["/graphql"]["post"]["x-rebrowse-graphql"]) == 5


def test_sibling_hosts_get_operation_servers(tmp_path):
    entries = [_home(), _get("/api/items", {"items": []}),
               _get("https://api.app.example.com/v1/me", {"id": 1}),
               _get("https://api.app.example.com/api/items", {"items": []})]
    both = [{"url": "https://api.app.example.com"}, {"url": SITE}]

    document = _har_document(tmp_path, entries)
    api = _har_document(tmp_path, entries, "--domain", "api.app.example.com")

    assert document["servers"] == [{"url": SITE}]
    assert document["paths"]["/v1/me"]["get"]["servers"] == [
        {"url": "https://api.app.example.com"}]
    assert document["paths"]["/api/items"]["get"]["servers"] == both
    assert api["servers"] == [{"url": "https://api.app.example.com"}]
    assert "servers" not in api["paths"]["/v1/me"]["get"]
    assert api["paths"]["/api/items"]["get"]["servers"] == both


def test_version_hashes_the_rest_and_entry_order_does_not_matter(tmp_path):
    entries = _app()
    shuffled = entries[:]
    random.Random(7).shuffle(shuffled)
    first, second = tmp_path / "first.json", tmp_path / "second.json"

    document = _document(_write(tmp_path, entries, "first.har"), first)
    _document(_write(tmp_path, shuffled, "second.har"), second)

    assert second.read_bytes() == first.read_bytes()
    info = document["info"]
    assert list(info) == ["title", "version"] and info["title"] == "app.example.com API"
    unversioned = {**document, "info": {"title": info["title"]}}
    canonical = json.dumps(unversioned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert info["version"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def test_a_recording_is_documented_without_a_skill_llm_or_network(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a recording is documented offline, without a skill")

    monkeypatch.setattr(llm, "describe_endpoints", boom)
    monkeypatch.setattr(store, "_encode", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)

    _document(_write(tmp_path, _app(), "some.har"), tmp_path / "x.json")

    assert not config.DB_PATH.exists()
    assert list_all_skills() == []


def test_out_prints_a_summary_and_stdout_gets_the_same_bytes(tmp_path):
    har = _write(tmp_path, _app())
    out = tmp_path / "api.json"

    code, summary = _invoke("openapi", str(har), "-o", str(out))
    piped = CliRunner().invoke(main, ["openapi", str(har)])

    assert code == 0 and summary == {"source": str(har.resolve()), "domain": "app.example.com",
                                     "path": str(out.resolve()), "paths": 10, "operations": 11}
    assert piped.exit_code == 0 and piped.stdout_bytes == out.read_bytes()
    assert out.read_bytes().endswith(b"}\n")


def test_a_file_wins_over_a_skill_of_the_same_name(tmp_path, monkeypatch):
    _skill("app.example.com")
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _app(), "app.example.com")

    result = CliRunner().invoke(main, ["openapi", "app.example.com"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["info"] == json.loads(
        CliRunner().invoke(main, ["openapi", str(tmp_path / "app.example.com")]).stdout)["info"]
    assert "x-rebrowse-skill-id" not in json.loads(result.stdout)["info"]


def test_domain_with_a_skill_target_is_a_usage_error():
    _skill("fx.local")

    result = CliRunner().invoke(main, ["openapi", "fx.local", "-d", "x"])

    assert result.exit_code == 2 and "--domain needs a recording file" in result.output


def test_unreadable_or_empty_recordings_exit_1_and_leave_no_file(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    neither = tmp_path / "neither.json"
    neither.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    static = _write(tmp_path, [
        _home(), _entry("POST", f"{SITE}/collect", post=_json_post({"event": "view"}), body="{}"),
        _get("https://api.stripe.com/v1/charges", {"data": []})], "static.har")
    out = tmp_path / "api.json"
    cases = [
        (broken, f"Cannot read {broken}"),
        (neither, f"{neither} is neither a HAR file nor a rebrowse capture"),
        (static, f"No API traffic for app.example.com in {static}"),
        (_write(tmp_path, [_home()], "home.har"), "No API traffic for app.example.com"),
    ]

    for source, error in cases:
        code, report = _invoke("openapi", str(source), "-o", str(out))
        assert code == 1 and error in report["error"], (source, report)
    assert not out.exists()


def test_out_in_a_missing_folder_exits_1_and_leaves_nothing(tmp_path):
    out = tmp_path / "missing" / "api.json"

    code, report = _invoke("openapi", str(_write(tmp_path, _app())), "-o", str(out))

    assert code == 1 and report["error"].startswith(f"Could not write {out}")
    assert not out.parent.exists()


def test_a_lone_surrogate_is_written_as_its_escape(tmp_path):
    har = _write(tmp_path, [_entry("GET", f"{SITE}/api/names?q=\ud800", body='{"n": 1}')])
    from_har, baseline, from_baseline = (tmp_path / n for n in ("a.json", "b.json", "c.json"))

    document = _document(har, from_har)
    assert _invoke("baseline", str(har), "-o", str(baseline))[0] == 0
    _document(baseline, from_baseline)

    assert from_baseline.read_bytes() == from_har.read_bytes()
    assert b'"example": "\\ud800"' in from_har.read_bytes()
    [param] = document["paths"]["/api/names"]["get"]["parameters"]
    assert param["example"] == "\ud800"


def test_openapi_module_imports_no_http_client():
    tree = ast.parse(Path(openapi.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}

    assert not imported & (HTTP_CLIENTS | {"rebrowse.contract"})


def test_xssi_guarded_json_gets_a_schema_beside_other_media(tmp_path):
    document = _har_document(tmp_path, [
        _home(),
        _entry("GET", f"{SITE}/api/guarded", body=")]}'\n{\"ok\": true}"),
        _get("/api/page", {"id": 1}),
        _entry("GET", f"{SITE}/api/page", mime="text/html; charset=utf-8", body="<p>sign in</p>"),
    ])

    assert _schema(document, "/api/guarded")["properties"] == {"ok": {"type": "boolean"}}
    assert document["paths"]["/api/page"]["get"]["responses"]["200"]["content"] == {
        "application/json": {"schema": {
            "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
        "text/html": {}}


def test_request_media_types_drop_parameters_and_json_sent_as_text_gets_a_schema(tmp_path):
    me = {"operationName": "Me", "query": "query Me { me { id } }"}
    document = _har_document(tmp_path, [
        _home(),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body='{"id": 1}',
               headers=[("Content-Type", "Application/JSON; charset=UTF-8")]),
        _entry("POST", f"{SITE}/graphql", post={"mimeType": "text/plain", "text": json.dumps(me)},
               body='{"data": {"me": {"id": 1}}}', headers=[("Content-Type", "text/plain")]),
    ])
    paths = document["paths"]

    assert paths["/api/notes"]["post"]["requestBody"]["content"] == {"application/json": {
        "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"]},
        "example": {"text": ""}}}
    text = paths["/graphql"]["post"]["requestBody"]["content"]["text/plain"]
    assert text["example"] == me and text["schema"]["required"] == ["operationName", "query"]


def test_write_query_values_are_blank_and_action_keys_set_the_effect(tmp_path):
    document = _har_document(tmp_path, [
        _home(), _entry("POST", f"{SITE}/api/run?note=SENTINEL&action=delete",
                        post=_json_post({"x": 1}), body="{}")])
    op = document["paths"]["/api/run"]["post"]

    assert [(p["name"], p["required"], p["example"]) for p in op["parameters"]] == [
        ("action", True, "delete"), ("note", True, "")]
    assert op["x-rebrowse-effect"] == "destructive"
    assert "SENTINEL" not in json.dumps(document)


def test_schemas_stop_at_drifts_max_depth(tmp_path):
    body: object = 1
    for _ in range(drift.MAX_DEPTH + 5):
        body = {"k": body}

    schema = _schema(_har_document(tmp_path, [_home(), _get("/api/deep", body)]), "/api/deep")

    for _ in range(drift.MAX_DEPTH):
        assert schema["required"] == ["k"]
        schema = schema["properties"]["k"]
    assert schema == {"type": "object"}


def test_domain_gives_the_same_bytes_from_a_har_and_its_baseline(tmp_path):
    har = _write(tmp_path, [_home(), _get("/api/items", {"items": []}),
                            _get("https://api.app.example.com/v1/me", {"id": 1})])
    from_har, baseline, from_baseline = (tmp_path / n for n in ("a.json", "b.json", "c.json"))
    domain = ("--domain", "api.app.example.com")

    _document(har, from_har, *domain)
    assert _invoke("baseline", str(har), "-o", str(baseline))[0] == 0
    _document(baseline, from_baseline, *domain)

    assert from_baseline.read_bytes() == from_har.read_bytes()
    document = json.loads(from_har.read_bytes())
    assert document["servers"] == [{"url": "https://api.app.example.com"}]
    assert document["paths"]["/api/items"]["get"]["servers"] == [{"url": SITE}]


def test_a_capture_saved_by_build_matches_its_baseline_without_bundle_routes(tmp_path):
    saved = save_capture(CaptureResult(
        domain="app.example.com", final_url=f"{SITE}/",
        js_bundles={f"{SITE}/app.js": 'fetch("/api/hidden")'},
        requests=[RawRequest(
            url=f"{SITE}/api/users/1001", method="GET", response_status=200,
            request_headers={"Cookie": "sid=SENTINEL-COOKIE"},
            response_headers={"content-type": "application/json"},
            response_body=json.dumps({"id": 1001, "email": "SENTINEL@corp.test"}))]))
    from_capture, baseline, from_baseline = (tmp_path / n for n in ("a.json", "b.json", "c.json"))

    code, summary = _invoke("openapi", str(saved), "-o", str(from_capture))
    assert _invoke("baseline", str(saved), "-o", str(baseline))[0] == 0
    _document(baseline, from_baseline)

    assert code == 0 and summary["source"] == str(saved.resolve())
    data = from_capture.read_bytes()
    assert from_baseline.read_bytes() == data
    assert list(json.loads(data)["paths"]) == ["/api/users/{users_id}"]
    assert b"SENTINEL" not in data


def test_a_skill_out_summary_keeps_its_skill_id(tmp_path):
    skill = _skill("fx.local")
    out = tmp_path / "api.json"

    code, summary = _invoke("openapi", "fx.local", "-o", str(out))

    assert code == 0 and summary == {"skill_id": skill.skill_id, "domain": "fx.local",
                                     "path": str(out.resolve()), "paths": 1, "operations": 1}


def test_required_matches_diff_inside_arrays_and_maps(tmp_path):
    base = _write(tmp_path, [_get("/api/orders", {
        "orders": [{"id": 1, "total": 9.5}, {"id": 2}],
        "members": {"ann@corp.test": {"role": "admin", "since": 2020},
                    "bob@corp.test": {"role": "dev"}},
    })], "base.har")
    head = _write(tmp_path, [_get("/api/orders", {
        "orders": [{"n": 1}], "members": {"ann@corp.test": {"n": 1}}})], "head.har")

    _, report = _invoke("diff", str(base), str(head))
    schema = _schema(_document(base, tmp_path / "api.json"), "/api/orders")

    removed = {c["field"]: c["severity"] for c in report["changes"] if c["kind"] == "field_removed"}
    assert removed == {"$.orders[].id": "breaking", "$.orders[].total": "info",
                       "$.members.*.role": "breaking", "$.members.*.since": "info"}
    assert schema["properties"]["orders"]["items"]["required"] == ["id"]
    assert schema["properties"]["members"]["additionalProperties"]["required"] == ["role"]


def test_required_is_per_status_across_merged_hosts_and_operations(tmp_path):
    def recording(full: bool, name: str) -> Path:
        return _write(tmp_path, [
            _home(),
            _entry("POST", f"{SITE}/api/notes", status=201, post=_json_post({"text": "a"}),
                   body=json.dumps({"id": 1, "created": 1700000000} if full else {"id": 1})),
            _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "b"}), body='{"id": 2}'),
            _get("/v1/me", {"id": 1, "email": "a@x.test"} if full else {"id": 1}),
            _get("https://api.app.example.com/v1/me", {"id": 1}),
            _entry("POST", f"{SITE}/graphql", post=_gql("User"),
                   body=json.dumps({"data": {"user": {"id": 1}} if full else {}})),
            _entry("POST", f"{SITE}/graphql", post=_gql("Feed"), body='{"data": {"feed": []}}'),
        ], name)

    base = recording(True, "base.har")
    _, report = _invoke("diff", str(base), str(recording(False, "head.har")))
    document = _document(base, tmp_path / "api.json")

    assert {(c["route"], c["field"]): c["severity"] for c in report["changes"]} == {
        ("POST /api/notes", "$.created"): "info",
        ("GET /v1/me", "$.email"): "breaking",
        ("POST /graphql [GraphQL query: User]", "$.data.user"): "breaking"}
    assert _schema(document, "/api/notes", "201", "post")["required"] == ["created", "id"]
    assert _schema(document, "/v1/me")["required"] == ["id"]
    assert "required" not in _schema(document, "/graphql", method="post")["properties"]["data"]


def test_destructive_calls_keep_their_effect_through_the_baseline(tmp_path):
    delete_note = {"operationName": "DeleteNote",
                   "query": "mutation DeleteNote($id: ID!) { deleteNote(id: $id) { ok } }",
                   "variables": {"id": "1001"}}
    document = _har_document(tmp_path, [
        _home(),
        _entry("POST", f"{SITE}/graphql", post=_json_post(ADD_NOTE), body='{"data": {}}'),
        _entry("POST", f"{SITE}/graphql", post=_json_post(delete_note), body='{"data": {}}'),
        _entry("DELETE", "https://api.app.example.com/v1/notes/1001", status=204, mime=""),
    ])
    post = document["paths"]["/graphql"]["post"]
    delete = document["paths"]["/v1/notes/{notes_id}"]["delete"]

    assert post["x-rebrowse-effect"] == "destructive"
    assert post["x-rebrowse-graphql"] == ["GraphQL mutation: AddNote", "GraphQL mutation: DeleteNote"]
    assert delete["x-rebrowse-effect"] == "destructive"
    assert delete["servers"] == [{"url": "https://api.app.example.com"}]


def test_preflights_and_head_requests_are_left_out(tmp_path):
    document = _har_document(tmp_path, [
        _home(),
        _entry("OPTIONS", "https://api.app.example.com/v1/me", status=204, mime="",
               headers=[("Access-Control-Request-Method", "GET")]),
        _get("https://api.app.example.com/v1/me", {"id": 1}),
        _entry("HEAD", f"{SITE}/api/items"),
        _get("/api/items", {"items": []}),
    ])

    assert {path: list(methods) for path, methods in document["paths"].items()} == {
        "/api/items": ["get"], "/v1/me": ["get"]}


def test_a_baseline_written_for_a_sibling_host_documents_that_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, [_home(), _get("/api/items", {"items": []}),
                      _get("https://api.app.example.com/v1/me", {"id": 1})])
    api = ("-d", "api.app.example.com")
    assert _invoke("baseline", "session.har", *api, "-o", "base.json")[0] == 0

    code, summary = _invoke("openapi", "base.json", "-o", "b.json")
    _document(Path("session.har"), Path("a.json"), *api)

    assert code == 0 and summary == {
        "source": str((tmp_path / "base.json").resolve()), "domain": "api.app.example.com",
        "path": str((tmp_path / "b.json").resolve()), "paths": 2, "operations": 2}
    assert (tmp_path / "b.json").read_bytes() == (tmp_path / "a.json").read_bytes()


def test_a_domain_missing_from_the_har_exits_1_and_leaves_no_file(tmp_path):
    out = tmp_path / "api.json"

    code, report = _invoke("openapi", str(_write(tmp_path, _app())), "-d", "nothere.test",
                           "-o", str(out))

    assert code == 1 and report["error"].startswith("No requests to nothere.test in the recording")
    assert not out.exists()


STATEFUL_MODULES = {"httpx", "requests", "aiohttp", "sqlite3", "sentence_transformers", "playwright",
                    "rebrowse.net", "rebrowse.execution.executor", "rebrowse.llm.client",
                    "rebrowse.store.skills", "rebrowse.auth.vault"}


def test_a_live_capture_saved_by_build_matches_its_baseline(fixture_site, tmp_path):
    from rebrowse.capture.browser import capture_session

    saved = save_capture(asyncio.run(capture_session(fixture_site + "/", timeout_ms=15000)))
    from_capture, baseline, from_baseline = (tmp_path / n for n in ("a.json", "b.json", "c.json"))

    document = _document(saved, from_capture)
    assert _invoke("baseline", str(saved), "-o", str(baseline))[0] == 0
    _document(baseline, from_baseline)

    assert from_baseline.read_bytes() == from_capture.read_bytes()
    assert_valid(document)
    assert {path: list(methods) for path, methods in document["paths"].items()} == {
        "/api/echo": ["get"], "/api/items": ["get"], "/api/notes": ["post"]}
    assert "abc123" not in from_capture.read_text(encoding="utf-8")
    assert not config.DB_PATH.exists()


def test_documenting_a_recording_loads_no_skill_store_llm_vault_or_http_client(tmp_path):
    har = _write(tmp_path, _app())
    data = tmp_path / "data"
    script = (
        "import json, sys\n"
        "from click.testing import CliRunner\n"
        "from rebrowse.cli import main\n"
        f"result = CliRunner().invoke(main, ['openapi', {str(har)!r}])\n"
        "print(json.dumps([result.exit_code, sorted(sys.modules)]))\n"
    )

    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], check=True,
                         env={**os.environ, "REBROWSE_DATA_DIR": str(data)})
    code, modules = json.loads(run.stdout.splitlines()[-1])

    assert code == 0
    assert not STATEFUL_MODULES & set(modules)
    assert not (data / "skills.db").exists()


def test_path_parameters_follow_placeholder_order_and_headers_sent_once_are_optional(tmp_path):
    document = _har_document(tmp_path, [
        _home(),
        _get("/api/orgs/1001/users/2002", {"id": 1}, headers=[("X-Client", "web")]),
        _get("/api/orgs/3003/users/4004", {"id": 2}),
    ])
    params = document["paths"]["/api/orgs/{orgs_id}/users/{users_id}"]["get"]["parameters"]

    assert [(p["in"], p["name"], p["required"], p["example"]) for p in params] == [
        ("path", "orgs_id", True, "1001"), ("path", "users_id", True, "2002"),
        ("header", "x-client", False, "web")]


def test_a_batched_graphql_call_names_each_operation(tmp_path):
    batch = [{"operationName": "A", "query": "query A { a }"},
             {"operationName": "B", "query": "query B { b }"}]
    document = _har_document(tmp_path, [
        _home(), _entry("POST", f"{SITE}/graphql", post=_json_post(batch),
                        body='[{"data": {"a": 1}}, {"data": {"b": 2}}]')])
    post = document["paths"]["/graphql"]["post"]

    assert post["x-rebrowse-graphql"] == ["GraphQL query: A", "GraphQL query: B"]
    media = post["requestBody"]["content"]["application/json"]
    assert media["schema"]["type"] == "array" and media["example"] == batch
    data = _schema(document, "/graphql", method="post")["items"]["properties"]["data"]
    assert sorted(data["properties"]) == ["a", "b"] and "required" not in data


def test_top_level_arrays_scalars_and_null_get_schemas(tmp_path):
    document = _har_document(tmp_path, [
        _home(), _get("/api/ids", [1, 2]), _get("/api/name", "SENTINEL"), _get("/api/none", None),
        _get("/api/mixed", [{"a": 1}, "x", None])])

    assert _schema(document, "/api/ids") == {"type": "array", "items": {"type": "integer"}}
    assert _schema(document, "/api/name") == {"type": "string"}
    assert _schema(document, "/api/none") == {"type": "null"}
    assert _schema(document, "/api/mixed")["items"] == {
        "type": ["null", "object", "string"], "properties": {"a": {"type": "integer"}},
        "required": ["a"]}
    assert "SENTINEL" not in json.dumps(document)


def test_a_domain_outside_a_saved_capture_exits_1(tmp_path):
    saved = save_capture(CaptureResult(domain="app.example.com", final_url=f"{SITE}/", requests=[
        RawRequest(url=f"{SITE}/api/items", method="GET", response_status=200,
                   response_headers={"content-type": "application/json"}, response_body="{}")]))

    code, report = _invoke("openapi", str(saved), "-d", "other.test")

    assert code == 1 and report["error"].endswith("is a capture of app.example.com, not other.test")


def test_an_error_without_out_is_json_on_stdout(tmp_path):
    har = _write(tmp_path, [_home()])

    result = CliRunner().invoke(main, ["openapi", str(har)])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": f"No API traffic for app.example.com in {har}"}


def test_odd_statuses_text_json_bodies_and_repeated_params_stay_valid():
    def call(status, **fields):
        return RawRequest(**{"url": "https://app.example.com/api/a?tag=x&tag=y", "method": "POST",
                             "request_body": '{"q": "cats"}',
                             "request_headers": {"content-type": "text/plain;charset=UTF-8"},
                             "response_status": status,
                             "response_headers": {"content-type": "application/json"},
                             "response_body": '{"ok": true}', **fields})

    tagged = RawRequest(url="https://app.example.com/api/b?tag=x&tag=y", method="GET",
                        response_status=200, response_headers={"content-type": "application/json"},
                        response_body="{}")
    capture = CaptureResult(domain="app.example.com", final_url="about:blank",
                            requests=[call(200), call(999), tagged])
    document = openapi.recording_to_openapi(capture)

    op = document["paths"]["/api/a"]["post"]
    assert list(op["responses"]) == ["200", "default"]
    assert op["requestBody"]["content"]["text/plain"]["schema"]["properties"] == {
        "q": {"type": "string"}}
    tags = document["paths"]["/api/b"]["get"]["parameters"]
    assert [p["example"] for p in tags if p["name"] == "tag"] == ["x"]
    assert document["servers"] == [{"url": "https://app.example.com"}]
    assert_valid(document)


def test_a_missing_recording_path_is_not_looked_up_as_a_skill(tmp_path):
    result = CliRunner().invoke(main, ["openapi", str(tmp_path / "missing.har")])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"].startswith("No such file:")
