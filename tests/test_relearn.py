from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import pytest
from click.testing import CliRunner
from test_har import _entry, _json_post, _write

from rebrowse import mcp_server
from rebrowse.capture.har import load_har
from rebrowse.cli import main
from rebrowse.llm.client import LLMError
from rebrowse.models import (
    EndpointDescriptor,
    HttpMethod,
    RawRequest,
    SkillManifest,
    VerificationStatus,
)
from rebrowse.openapi import skill_to_openapi
from rebrowse.orchestrator import pipeline
from rebrowse.reverse.extractor import endpoint_key, extract_endpoints
from rebrowse.safety import Effect
from rebrowse.store.skills import resolve_skill, save_skill

SITE = "http://app.local"
VERIFIED, UNVERIFIED = VerificationStatus.VERIFIED, VerificationStatus.UNVERIFIED
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def describe(monkeypatch) -> list:
    calls: list = []

    async def stub(endpoints):
        calls.append(endpoints)
        return [{**e, "description": f"v{len(calls)}: {e['method']} {e['url_template']}"}
                for e in endpoints]

    monkeypatch.setattr(pipeline, "describe_endpoints", stub)
    return calls


def _session(items: str = '{"items": [{"id": 1}]}') -> list[dict]:
    return [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{SITE}/static/app.js", mime="application/javascript",
               body='fetch("/api/recommendations")'),
        _entry("GET", f"{SITE}/api/items", body=items),
        _entry("GET", f"{SITE}/api/users/101", body='{"id": 101, "name": "ann"}'),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"text": "hi"}), body='{"ok": true}'),
        _entry("POST", f"{SITE}/graphql", body='{"data": {"feed": []}}', post=_json_post(
            {"operationName": "Feed", "query": "query Feed { feed { id } }"})),
    ]


TAGS = _entry("GET", f"{SITE}/api/tags", body='{"tags": []}')


def _row(result: dict, method: str, path: str) -> dict:
    return next(row for row in result["endpoints"]
                if row["method"] == method and urlsplit(row["url"]).path == path)


def _ids(result: dict) -> set[str]:
    return {row["id"] for row in result["endpoints"]}


def _ep(path: str, **fields) -> EndpointDescriptor:
    return EndpointDescriptor(url_template=f"{SITE}{path}", **fields)


def _graphql(url: str, payload) -> RawRequest:
    return RawRequest(url=url, method="POST", request_headers={"content-type": "application/json"},
                      request_body=json.dumps(payload), response_status=200,
                      response_headers={"content-type": "application/json"},
                      response_body='{"data": {}}')


def test_endpoint_key_ignores_placeholder_names_only():
    traffic = _ep("/api/users/{users_id}")
    key = endpoint_key(traffic)

    assert key == endpoint_key(_ep("/api/users/{id}"))
    assert key != endpoint_key(EndpointDescriptor(url_template="http://api.local/api/users/{id}"))
    assert key != endpoint_key(traffic.model_copy(update={"method": HttpMethod.DELETE}))


def test_endpoint_key_separates_graphql_operations_and_survives_storage():
    url = "https://ex.com/graphql"
    endpoints = extract_endpoints([
        _graphql(url, {"operationName": "Q2", "query": "query Q2 { a }"}),
        _graphql(url, {"extensions": {"persistedQuery": {"version": 1, "sha256Hash": "ab" * 32}}}),
        RawRequest(url=f"{url}?query={quote('{ me { id } }')}", method="GET", response_status=200,
                   response_headers={"content-type": "application/json"}, response_body="{}"),
        _graphql(url, [{"operationName": "A", "query": "query A { a }"},
                       {"operationName": "B", "query": "mutation B { b }"}]),
    ], page_domain="ex.com")

    keys = [endpoint_key(ep) for ep in endpoints]
    stored = [endpoint_key(EndpointDescriptor.model_validate_json(ep.model_dump_json()))
              for ep in endpoints]

    assert stored == keys
    assert len(set(keys)) == len(endpoints) == 5
    tokens = {token for _, _, token in keys}
    assert {"gql:Q2", f"gql#{'ab' * 8}", "gql:A", "gql:B"} < tokens
    assert any(token.startswith("gqlq:") for token in tokens)


def test_carry_forward_keeps_identity_descriptions_and_matching_health():
    trigger = {"trigger_url": f"{SITE}/x"}
    previous = [
        _ep("/api/a", endpoint_id="a1", description="old A", verification_status=VERIFIED,
            reliability_score=0.9, query={"page": "1"}, headers_template={"x-old": "1"}, **trigger),
        _ep("/api/b", endpoint_id="b1", description="JS bundle route: /api/b"),
        _ep("/api/c", endpoint_id="c1", method=HttpMethod.POST, effect=Effect.READ,
            description="old C", verification_status=VERIFIED, reliability_score=0.8, **trigger),
        _ep("/api/e", endpoint_id="e1", description="old E", **trigger),
    ]
    a, b, c, d = endpoints = [
        _ep("/api/a", query={"page": "2"}, **trigger),
        _ep("/api/b", **trigger),
        _ep("/api/c", method=HttpMethod.POST, effect=Effect.WRITE, **trigger),
        _ep("/api/d", **trigger),
    ]

    dropped = pipeline._carry_forward(endpoints, previous)[1]

    assert (a.endpoint_id, a.description, a.verification_status, a.reliability_score) == (
        "a1", "old A", VERIFIED, 0.9)
    assert (a.query, a.headers_template) == ({"page": "2"}, {})
    assert (b.endpoint_id, b.description) == ("b1", None)
    assert (c.endpoint_id, c.description, c.verification_status, c.reliability_score) == (
        "c1", "old C", UNVERIFIED, 0.5)
    assert d.endpoint_id not in {"a1", "b1", "c1", "e1"}
    assert dropped == [previous[3]]


def test_health_is_not_carried_between_bundle_routes_and_observed_traffic():
    previous = [
        _ep("/api/items/{id}", endpoint_id="i1", verification_status=VerificationStatus.FAILED,
            reliability_score=0.15),
        _ep("/api/users/{users_id}", endpoint_id="u1", verification_status=VERIFIED,
            reliability_score=0.9, trigger_url=f"{SITE}/"),
    ]
    observed = _ep("/api/items/{items_id}", reliability_score=0.8, trigger_url=f"{SITE}/")
    bundled = _ep("/api/users/{id}", reliability_score=0.3)

    pipeline._carry_forward([observed, bundled], previous)[1]

    assert [(ep.endpoint_id, ep.verification_status, ep.reliability_score)
            for ep in (observed, bundled)] == [("i1", UNVERIFIED, 0.8), ("u1", UNVERIFIED, 0.3)]


def test_legacy_duplicates_are_each_carried_at_most_once():
    previous = [_ep("/api/users/{id}", endpoint_id="u1"),
                _ep("/api/users/{users_id}", endpoint_id="u2")]
    single = [_ep("/api/users/{users_id}"), _ep("/api/teams")]
    twice = [_ep("/api/users/{users_id}"), _ep("/api/users/{users_id}")]

    assert [ep.endpoint_id for ep in pipeline._carry_forward(single, previous)[1]] == ["u2"]
    assert single[0].endpoint_id == "u1" and single[1].endpoint_id not in {"u1", "u2"}
    assert pipeline._carry_forward(twice, previous)[1] == []
    assert [ep.endpoint_id for ep in twice] == ["u1", "u2"]


async def test_reimporting_the_same_har_keeps_ids_and_skips_the_llm(tmp_path, describe):
    har = _write(tmp_path, _session())

    first = await pipeline.import_har(har)
    again = await pipeline.import_har(har)

    count = first["endpoints_count"]
    assert first["changes"] == {"added": count, "kept": 0, "dropped": 0, "described": count - 1}
    assert {row["change"] for row in first["endpoints"]} == {"added"}
    assert len(describe) == 1
    assert again["changes"] == {"added": 0, "kept": count, "dropped": 0, "described": 0}
    assert {row["change"] for row in again["endpoints"]} == {"kept"}
    assert again["dropped"] == []
    descriptions = {row["id"]: row["description"] for row in first["endpoints"]}
    assert {row["id"]: row["description"] for row in again["endpoints"]} == descriptions
    assert _row(again, "GET", "/api/items")["description"].startswith("v1: ")


async def test_reimport_describes_only_new_endpoints_and_reports_dropped(tmp_path, describe):
    first = await pipeline.import_har(_write(tmp_path, _session()))
    notes = _row(first, "POST", "/api/notes")
    v2 = [e for e in _session() if not e["request"]["url"].endswith("/api/notes")] + [TAGS]

    again = await pipeline.import_har(_write(tmp_path, v2, "v2.har"))

    assert [(e["method"], e["url_template"]) for e in describe[1]] == [("GET", f"{SITE}/api/tags")]
    assert again["changes"] == {"added": 1, "kept": first["endpoints_count"] - 1, "dropped": 1,
                                "described": 1}
    assert again["dropped"] == [{k: v for k, v in notes.items() if k != "change"}]
    assert _row(again, "GET", "/api/tags")["change"] == "added"
    assert _ids(first) - {notes["id"]} < _ids(again)


async def test_verify_results_survive_a_reimport(tmp_path, fixture_site, describe):
    har = _write(tmp_path, [
        _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{fixture_site}/api/items", body='{"items": [{"id": 1}]}'),
        _entry("POST", f"{fixture_site}/api/notes", post=_json_post({"text": "hi"}),
               body='{"ok": true}'),
    ])
    domain = urlsplit(fixture_site).netloc
    await pipeline.import_har(har)
    assert (await pipeline.verify(domain))["verified"] == 1

    def items() -> EndpointDescriptor:
        return next(ep for ep in resolve_skill(domain).endpoints
                    if ep.url_template.endswith("/api/items"))

    before = items()
    await pipeline.import_har(har)
    after = items()

    assert after.verification_status is VERIFIED
    assert (after.endpoint_id, after.reliability_score) == (
        before.endpoint_id, before.reliability_score)
    spec = skill_to_openapi(resolve_skill(domain))
    assert spec["paths"]["/api/items"]["get"]["x-rebrowse-verification"] == "verified"


async def test_llm_outage_on_reimport_keeps_descriptions(tmp_path, describe, monkeypatch):
    first = await pipeline.import_har(_write(tmp_path, _session()))

    async def unreachable(endpoints):
        raise LLMError("ConnectError: All connection attempts failed")

    monkeypatch.setattr(pipeline, "describe_endpoints", unreachable)
    again = await pipeline.import_har(_write(tmp_path, [*_session(), TAGS], "v2.har"))

    kept = {row["id"]: row["description"] for row in again["endpoints"] if row["change"] == "kept"}
    assert kept == {row["id"]: row["description"] for row in first["endpoints"]}
    assert _row(again, "GET", "/api/tags")["description"] == "(no description)"
    assert again["changes"]["described"] == 0
    assert {ep.endpoint_id for ep in resolve_skill("app.local").endpoints} == _ids(again)


async def test_rebuilds_describe_the_next_batch(tmp_path, describe, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_DESCRIBE", 2)
    har = _write(tmp_path, [_entry("GET", f"{SITE}/api/r{i}", body='{"ok": 1}') for i in range(5)])

    first = await pipeline.import_har(har)
    again = await pipeline.import_har(har)

    batch = {row["id"]: row["description"] for row in first["endpoints"]
             if row["description"].startswith("v1: ")}
    assert len(batch) == 2 and first["changes"]["described"] == 2
    descriptions = {row["id"]: row["description"] for row in again["endpoints"]}
    assert {i: descriptions[i] for i in batch} == batch
    assert sum(text.startswith("v2: ") for text in descriptions.values()) == 2
    assert again["changes"]["described"] == 2
    assert not {e["url_template"] for e in describe[0]} & {e["url_template"] for e in describe[1]}


async def test_reimport_exports_identical_bytes_until_the_api_changes(
        tmp_path, describe, monkeypatch):
    har = _write(tmp_path, _session())
    await pipeline.import_har(har)
    first = json.dumps(skill_to_openapi(resolve_skill("app.local")))

    async def other(endpoints):
        return [{**e, "description": "a different wording"} for e in endpoints]

    monkeypatch.setattr(pipeline, "describe_endpoints", other)
    await pipeline.import_har(har)
    assert json.dumps(skill_to_openapi(resolve_skill("app.local"))) == first

    changed = _session(items='{"items": [{"id": 1, "title": "new"}]}')
    await pipeline.import_har(_write(tmp_path, changed, "changed.har"))
    version = skill_to_openapi(resolve_skill("app.local"))["info"]["version"]
    assert version != json.loads(first)["info"]["version"]


def test_cli_reimport_reports_kept_endpoints_and_exports_identical_files(tmp_path, describe):
    runner = CliRunner()
    har = _write(tmp_path, _session())
    before, after = tmp_path / "a.json", tmp_path / "b.json"

    assert runner.invoke(main, ["import-har", str(har)]).exit_code == 0
    assert runner.invoke(main, ["openapi", "app.local", "-o", str(before)]).exit_code == 0
    result = runner.invoke(main, ["import-har", str(har)])
    assert runner.invoke(main, ["openapi", "app.local", "-o", str(after)]).exit_code == 0

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["changes"]["kept"] == payload["endpoints_count"]
    assert before.read_bytes() == after.read_bytes()


async def test_a_stored_bundle_route_keeps_its_id_once_observed(tmp_path, describe):
    save_skill(SkillManifest(name="app.local API", domain="app.local", endpoints=[
        _ep("/api/users/{id}", endpoint_id="saved1", description="a user"),
    ]))

    result = await pipeline.import_har(_write(tmp_path, _session()))

    users = _row(result, "GET", "/api/users/{users_id}")
    assert (users["id"], users["description"], users["change"]) == ("saved1", "a user", "kept")
    assert result["changes"]["kept"] == 1


async def test_failed_verify_results_survive_a_reimport(tmp_path, fixture_site, describe):
    har = _write(tmp_path, [
        _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>"),
        _entry("GET", f"{fixture_site}/api/fail", body='{"ok": true}'),
    ])
    domain = urlsplit(fixture_site).netloc
    await pipeline.import_har(har)
    assert (await pipeline.verify(domain))["failed"] == 1
    [before] = resolve_skill(domain).endpoints

    again = await pipeline.import_har(har)
    [after] = resolve_skill(domain).endpoints

    assert after.verification_status is VerificationStatus.FAILED
    assert (after.endpoint_id, after.reliability_score) == (
        before.endpoint_id, before.reliability_score)
    assert again["endpoints"][0]["change"] == "kept"
    spec = skill_to_openapi(resolve_skill(domain))
    assert spec["paths"]["/api/fail"]["get"]["x-rebrowse-verification"] == "failed"


async def test_a_failed_bundle_route_starts_unverified_once_observed(
        tmp_path, fixture_site, describe):
    page = _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>")
    bundle = _entry("GET", f"{fixture_site}/static/app.js", mime="application/javascript",
                    body="function item(id){ return fetch(`/api/items/${id}`); }")
    domain = urlsplit(fixture_site).netloc
    await pipeline.import_har(_write(tmp_path, [page, bundle]))
    assert (await pipeline.verify(domain))["failed"] == 1
    [before] = resolve_skill(domain).endpoints
    observed = _write(tmp_path, [
        page, _entry("GET", f"{fixture_site}/api/items/12345", body='{"id": 12345}'),
    ], "observed.har")

    again = await pipeline.import_har(observed)
    [after] = resolve_skill(domain).endpoints

    [fresh] = extract_endpoints(load_har(observed).requests, page_domain=domain)
    assert (before.verification_status, before.reliability_score) == (
        VerificationStatus.FAILED, 0.15)
    assert (after.endpoint_id, again["endpoints"][0]["change"]) == (before.endpoint_id, "kept")
    assert (after.verification_status, after.reliability_score) == (
        UNVERIFIED, fresh.reliability_score)
    operation = skill_to_openapi(resolve_skill(domain))["paths"]["/api/items/{items_id}"]["get"]
    assert operation["x-rebrowse-verification"] == "unverified"


async def test_a_query_that_became_a_mutation_keeps_its_id_but_not_its_badge(
        tmp_path, describe, capsys):
    first = await pipeline.import_har(_write(tmp_path, _session()))
    skill = resolve_skill("app.local")
    for ep in skill.endpoints:
        ep.verification_status, ep.reliability_score = VERIFIED, 0.95
    save_skill(skill)
    mutation = "mutation Feed { feed { id title } }"
    changed = [e for e in _session() if not e["request"]["url"].endswith("/graphql")] + [
        _entry("POST", f"{SITE}/graphql", body='{"data": {"feed": []}}',
               post=_json_post({"operationName": "Feed", "query": mutation}))]

    again = await pipeline.import_har(_write(tmp_path, changed, "v2.har"))

    count = first["endpoints_count"]
    assert (f"{count} endpoints: 0 added, {count} kept, 0 dropped, 1 changed effect"
            in capsys.readouterr().err)
    feed = _row(again, "POST", "/graphql")
    assert (feed["id"], feed["change"]) == (_row(first, "POST", "/graphql")["id"], "kept")
    assert again["effect_changes"] == [{"id": feed["id"], "from": "read", "to": "write"}]
    assert (feed["effect"], feed["description"]) == ("write", "GraphQL mutation: Feed")
    stored = {ep.endpoint_id: ep for ep in resolve_skill("app.local").endpoints}
    assert stored[feed["id"]].verification_status is UNVERIFIED
    assert stored[feed["id"]].body["query"] == mutation
    items = stored[_row(again, "GET", "/api/items")["id"]]
    assert (items.verification_status, items.reliability_score) == (VERIFIED, 0.95)


async def test_a_bundle_placeholder_is_described_once_the_llm_is_back(
        tmp_path, describe, monkeypatch):
    working = pipeline.describe_endpoints

    async def unreachable(endpoints):
        raise LLMError("ConnectError: All connection attempts failed")

    monkeypatch.setattr(pipeline, "describe_endpoints", unreachable)
    har = _write(tmp_path, _session())
    first = await pipeline.import_har(har)
    exported = json.dumps(skill_to_openapi(resolve_skill("app.local")))
    again = await pipeline.import_har(har)
    placeholder = "JS bundle route: /api/recommendations"

    assert _row(first, "GET", "/api/recommendations")["description"] == placeholder
    assert _row(again, "GET", "/api/recommendations")["description"] == placeholder
    assert json.dumps(skill_to_openapi(resolve_skill("app.local"))) == exported

    monkeypatch.setattr(pipeline, "describe_endpoints", working)
    third = await pipeline.import_har(har)

    recommendations = _row(third, "GET", "/api/recommendations")
    assert recommendations["description"].startswith("v1: GET ")
    assert recommendations["id"] == _row(first, "GET", "/api/recommendations")["id"]
    assert third["changes"]["described"] == len(describe[0]) == first["endpoints_count"] - 1


async def test_get_graphql_operations_keep_their_ids(tmp_path, describe):
    persisted = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "cd" * 32}})
    named = {"operationName": "Me", "query": "query Me { me { id } }"}
    entries = [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        *(_entry("GET", f"{SITE}/graphql?{urlencode(params)}", body='{"data": {}}')
          for params in (named, {"extensions": persisted}, {"query": "{ viewer { id } }"})),
    ]
    har = _write(tmp_path, entries)

    first = await pipeline.import_har(har)
    again = await pipeline.import_har(har)

    assert first["endpoints_count"] == 3
    assert again["changes"] == {"added": 0, "kept": 3, "dropped": 0, "described": 0}
    assert _ids(again) == _ids(first)
    assert len({endpoint_key(ep) for ep in resolve_skill("app.local").endpoints}) == 3


async def test_a_skill_saved_before_effects_were_stored_carries_its_verify_result(
        tmp_path, describe):
    save_skill(SkillManifest(name="app.local API", domain="app.local", endpoints=[
        _ep("/api/items", endpoint_id="old1", description="all items", effect=None,
            verification_status=VERIFIED, reliability_score=0.77, trigger_url=f"{SITE}/"),
    ]))

    await pipeline.import_har(_write(tmp_path, _session()))

    [items] = [ep for ep in resolve_skill("app.local").endpoints if ep.endpoint_id == "old1"]
    assert (items.description, items.verification_status, items.reliability_score) == (
        "all items", VERIFIED, 0.77)
    assert items.url_template == f"{SITE}/api/items" and items.effect is Effect.READ


async def test_an_agents_endpoint_id_still_reads_after_a_reimport(tmp_path, fixture_site, describe):
    page = _entry("GET", f"{fixture_site}/", mime="text/html", body="<html></html>")
    items = _entry("GET", f"{fixture_site}/api/items", body='{"items": [{"id": 1}]}')
    first = await pipeline.import_har(_write(tmp_path, [page, items]))
    [op] = await mcp_server.list_operations(first["skill_id"])

    search = _entry("GET", f"{fixture_site}/api/search?q=cats", body='{"results": []}')
    again = await pipeline.import_har(_write(tmp_path, [page, items, search], "v2.har"))
    result = await mcp_server.call(first["skill_id"], op.endpoint_id)

    assert again["changes"]["added"] == 1
    assert result.success is True and "items" in result.result


async def test_a_recording_without_api_calls_leaves_the_skill_alone(tmp_path, describe):
    await pipeline.import_har(_write(tmp_path, _session()))
    before = resolve_skill("app.local").model_dump()

    empty = await pipeline.import_har(_write(tmp_path, _session()[:1], "empty.har"))

    assert empty["error"] == "No API endpoints found on app.local"
    assert resolve_skill("app.local").model_dump() == before


async def test_a_sibling_sites_skill_is_never_carried_over(tmp_path, describe):
    shop = await pipeline.import_har(_write(tmp_path, [
        _entry("GET", "http://shop.app.local/", mime="text/html", body="<html></html>"),
        _entry("GET", "http://shop.app.local/api/items", body='{"items": []}'),
    ], "shop.har"))

    result = await pipeline.import_har(_write(tmp_path, _session()))

    count = result["endpoints_count"]
    assert (result["domain"], result["replaced"]) == ("app.local", False)
    assert result["changes"] == {"added": count, "kept": 0, "dropped": 0, "described": count - 1}
    assert not _ids(result) & _ids(shop)
    assert {ep.endpoint_id for ep in resolve_skill("shop.app.local").endpoints} == _ids(shop)


async def test_endpoints_the_llm_skipped_are_asked_about_again(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    async def first_only(endpoints):
        calls.append([e["url_template"] for e in endpoints])
        return [{**endpoints[0], "description": f"call {len(calls)}"}]

    monkeypatch.setattr(pipeline, "describe_endpoints", first_only)
    har = _write(tmp_path, [_entry("GET", f"{SITE}/api/r{i}", body='{"ok": 1}') for i in range(3)])

    first = await pipeline.import_har(har)
    again = await pipeline.import_har(har)

    assert first["changes"]["described"] == again["changes"]["described"] == 1
    assert calls[1] == calls[0][1:]
    assert sorted(row["description"] for row in again["endpoints"]) == [
        "(no description)", "call 1", "call 2"]


async def test_a_kept_endpoint_replays_the_new_recordings_body(tmp_path, describe):
    first = await pipeline.import_har(_write(tmp_path, _session()))
    version = skill_to_openapi(resolve_skill("app.local"))["info"]["version"]
    pinned = {"text": "hi", "pinned": True}
    changed = [e for e in _session() if not e["request"]["url"].endswith("/api/notes")] + [
        _entry("POST", f"{SITE}/api/notes", post=_json_post(pinned), body='{"ok": true}')]

    again = await pipeline.import_har(_write(tmp_path, changed, "v2.har"))

    old, new = _row(first, "POST", "/api/notes"), _row(again, "POST", "/api/notes")
    assert (new["id"], new["description"], new["change"]) == (old["id"], old["description"], "kept")
    [notes] = [ep for ep in resolve_skill("app.local").endpoints if ep.endpoint_id == old["id"]]
    assert notes.body == pinned
    assert skill_to_openapi(resolve_skill("app.local"))["info"]["version"] != version


async def test_the_exported_version_does_not_depend_on_the_process(tmp_path, isolated, describe):
    await pipeline.import_har(_write(tmp_path, [
        *_session(),
        _entry("GET", f"{SITE}/api/search?q=cats&sort=new&page=2&lang=en", body='{"hits": []}',
               headers=[("X-Client", "web"), ("X-Build", "42"), ("Accept", "application/json")]),
    ]))
    expected = json.dumps(skill_to_openapi(resolve_skill("app.local")), indent=2,
                          ensure_ascii=False) + "\n"

    def export(seed: str) -> str:
        env = {**os.environ, "REBROWSE_DATA_DIR": str(isolated), "PYTHONHASHSEED": seed}
        done = subprocess.run([sys.executable, "-m", "rebrowse", "openapi", "app.local"],
                              cwd=REPO, env=env, capture_output=True, check=True, timeout=120)
        return done.stdout.decode("utf-8").replace("\r\n", "\n")

    assert export("1") == export("2") == expected


async def test_lone_surrogates_in_bodies_do_not_break_import(tmp_path, describe):
    lone = '{"text": "\\ud83d", "\\udc00": 1}'
    har = _write(tmp_path, [
        _entry("GET", f"{SITE}/", mime="text/html", body="<html></html>"),
        _entry("POST", f"{SITE}/api/notes", post=_json_post({"x": 1}), body=lone),
        _entry("POST", f"{SITE}/api/echo", post={"mimeType": "application/json", "text": lone},
               body='{"ok": true}'),
    ])

    result = await pipeline.import_har(har)

    assert result["endpoints_count"] == 2
    echo = next(ep for ep in resolve_skill("app.local").endpoints if ep.url_template.endswith("echo"))
    assert echo.body == {"text": "?", "?": 1}


def test_required_fields_do_not_depend_on_sample_order():
    def skill(*bodies: str) -> SkillManifest:
        requests = [RawRequest(url=f"{SITE}/api/feed", method="GET", response_status=200,
                               response_headers={"content-type": "application/json"},
                               response_body=body) for body in bodies]
        return SkillManifest(skill_id="s1", name="t", domain="app.local", updated_at="x",
                             endpoints=extract_endpoints(requests, page_domain="app.local"))

    a, b = '{"title": "t", "id": 1}', '{"id": 2, "title": "u"}'
    first, second = skill_to_openapi(skill(a, b)), skill_to_openapi(skill(b, a))

    assert first["info"]["version"] == second["info"]["version"]
