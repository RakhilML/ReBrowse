from __future__ import annotations

import json

import pytest

from rebrowse import config
from rebrowse.auth.vault import get_cookies
from rebrowse.capture import browser
from rebrowse.llm.client import LLMError
from rebrowse.models import CaptureResult
from rebrowse.openapi import skill_to_openapi
from rebrowse.orchestrator import pipeline
from rebrowse.reverse.extractor import canonical_template
from rebrowse.store.skills import list_all_skills, resolve_skill


@pytest.fixture
def llm(monkeypatch):
    state: dict = {"intent": {}, "pick": lambda candidates: {}}

    async def describe(endpoints):
        return [{**e, "description": f"stub: {e['method']} {e['url_template']}"} for e in endpoints]

    async def parse(prompt):
        return state["intent"]

    async def pick(intent, candidates):
        state["intent_text"], state["candidates"] = intent, candidates
        return state["pick"](candidates)

    monkeypatch.setattr(pipeline, "describe_endpoints", describe)
    monkeypatch.setattr(pipeline, "parse_intent", parse)
    monkeypatch.setattr(pipeline, "pick_endpoint", pick)
    return state


@pytest.fixture
def fast_capture(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_SETTLE_S", 0.4)
    original = browser._scroll_page

    async def quick_scroll(page, scrolls=1, delay=0.05):
        await original(page, scrolls=1, delay=0.05)

    monkeypatch.setattr(browser, "_scroll_page", quick_scroll)


@pytest.fixture
async def built(fixture_site, llm, fast_capture):
    return await pipeline.build(fixture_site + "/")


def _by_path(endpoints: list[dict]) -> dict[tuple[str, str], dict]:
    return {(e["method"], e["url"].split("/", 3)[-1]): e for e in endpoints}


def _pick_path(method: str, suffix: str):
    def choose(candidates):
        ep = next(c for c in candidates
                  if c["method"] == method and c["url_template"].endswith(suffix))
        return {"endpoint_id": ep["endpoint_id"]}
    return choose


async def test_build_extracts_traffic_and_bundle_routes(built, isolated):
    eps = _by_path(built["endpoints"])
    assert eps[("GET", "api/items")]["effect"] == "read"
    assert eps[("POST", "api/notes")]["effect"] == "write"
    assert ("GET", "api/recommendations") in eps
    assert ("GET", "api/users/{id}/posts") in eps
    assert eps[("POST", "api/comments/delete")]["effect"] == "destructive"
    assert not any(path.startswith("api/collect") for _, path in eps)
    assert ("GET", "") not in eps
    assert all(e["description"].startswith("stub:") for e in built["endpoints"])
    assert {e["change"] for e in built["endpoints"]} == {"added"}

    assert list((isolated / "captures").glob("*.json"))
    assert any(c["name"] == "sid" for c in get_cookies(built["domain"]))


def test_bundle_routes_matching_a_known_template_are_skipped():
    capture = CaptureResult(domain="app.local", final_url="http://app.local/", js_bundles={
        "http://app.local/app.js": "fetch(`/api/users/${id}/posts`); fetch(`/api/teams/${team}`);"
                                   " fetch(`/api/teams/${teamId}`);",
    })
    known = {("GET", canonical_template("http://app.local/api/users/{users_id}/posts"))}

    routes = pipeline._bundle_endpoints(capture, known)

    assert [ep.url_template for ep in routes] == ["http://app.local/api/teams/{team}"]


async def test_rebuild_replaces_instead_of_duplicating(built, fixture_site):
    again = await pipeline.build(fixture_site + "/")
    assert again["replaced"] is True
    assert again["skill_id"] == built["skill_id"]
    assert len(list_all_skills()) == 1
    assert {e["id"] for e in built["endpoints"]} <= {e["id"] for e in again["endpoints"]}
    assert again["changes"]["added"] == 0


async def test_run_executes_a_read(built, llm):
    llm["intent"] = {"domain": built["domain"], "action": "list items", "params": {}}
    llm["pick"] = _pick_path("GET", "/api/items")
    out = await pipeline.run("list the items")
    assert out["success"] is True and out["effect"] == "read"
    assert len(out["result"]["items"]) == 3
    assert all(isinstance(c["query_params"], list) for c in llm["candidates"])


async def test_dry_run_sends_nothing(built, llm, hits):
    llm["intent"] = {"domain": built["domain"], "action": "list items"}
    llm["pick"] = _pick_path("GET", "/api/items")
    out = await pipeline.run("list the items", dry_run=True)
    assert out["dry_run"] is True and out["plan"]["effect"] == "read"
    assert hits[("GET", "/api/items")] == 0


async def test_write_needs_confirmation(built, llm, hits):
    llm["intent"] = {"domain": built["domain"], "action": "add a note"}
    llm["pick"] = _pick_path("POST", "/api/notes")
    blocked = await pipeline.run("add a note")
    assert blocked["confirmation_required"] is True
    assert hits[("POST", "/api/notes")] == 0

    done = await pipeline.run("add a note", assume_yes=True)
    assert done["success"] is True
    assert hits[("POST", "/api/notes")] == 1
    assert done["result"]["received"].replace(" ", "") == '{"text":"hi"}'


async def test_picker_failure_falls_back_to_a_read(built, llm):
    def boom(candidates):
        raise LLMError("model unavailable")
    llm["intent"] = {"domain": built["domain"], "action": "anything"}
    llm["pick"] = boom
    out = await pipeline.run("anything")
    assert out["effect"] == "read"
    assert out["endpoint"].startswith("GET ")


async def test_malformed_llm_output_is_tolerated(built, llm):
    llm["intent"] = {"domain": built["domain"], "action": "items", "params": "not-a-dict"}
    llm["pick"] = lambda c: {"endpoint_id": next(x for x in c if x["url_template"].endswith(
        "/api/items"))["endpoint_id"], "params": None, "query": ["junk"]}
    out = await pipeline.run("items")
    assert out["success"] is True


async def test_intent_params_reach_the_picker(built, llm):
    llm["intent"] = {"domain": built["domain"], "action": "search", "params": {"q": "cats"}}
    llm["pick"] = _pick_path("GET", "/api/items")
    await pipeline.run("search cats", dry_run=True)
    assert '"q": "cats"' in llm["intent_text"]


async def test_unrelated_prompt_is_refused(built, llm, hits):
    llm["intent"] = {"domain": None, "action": "weather forecast for paris tomorrow"}
    out = await pipeline.run("weather in paris")
    assert "refusing to guess" in out["error"]
    assert sum(hits.values()) == 0


async def test_run_with_empty_store(llm):
    llm["intent"] = {"domain": "nowhere.example", "action": "anything"}
    out = await pipeline.run("anything")
    assert out["error"].startswith("No skills stored")


async def test_built_skill_exports_without_browser_headers_or_cookies(built):
    spec = skill_to_openapi(resolve_skill(built["skill_id"]))
    ops = {(path, method): op for path, methods in spec["paths"].items()
           for method, op in methods.items()}

    assert "abc123" not in json.dumps(spec)
    assert not [p for op in ops.values() for p in op.get("parameters", []) if p["in"] == "header"]
    notes = ops[("/api/notes", "post")]["requestBody"]["content"]["application/json"]
    assert notes["example"] == {"text": "hi"}
    assert ops[("/api/comments/delete", "post")]["x-rebrowse-effect"] == "destructive"
    assert ops[("/api/users/{id}/posts", "get")]["x-rebrowse-observed"] is False
    assert ops[("/api/items", "get")]["x-rebrowse-observed"] is True
