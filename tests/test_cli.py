from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from rebrowse.cli import main
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.orchestrator import pipeline
from rebrowse.store.skills import save_skill


@pytest.fixture
def runner():
    return CliRunner()


def test_skills_empty(runner):
    result = runner.invoke(main, ["skills"])
    assert result.exit_code == 0 and "No skills stored" in result.output


def test_missing_skill_exits_nonzero(runner):
    assert runner.invoke(main, ["show", "nope"]).exit_code == 1
    assert runner.invoke(main, ["delete", "nope"]).exit_code == 1


def test_build_rejects_bad_steps_before_launching(runner):
    result = runner.invoke(main, ["build", "example.com", "--steps", "frobnicate #x"])
    assert result.exit_code == 2 and "unknown step" in result.output


def test_run_dry_run_prints_clean_json(runner, fixture_site, monkeypatch):
    skill = SkillManifest(name="fx", domain="fx.local", endpoints=[
        EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/items")])
    save_skill(skill)

    async def parse(prompt):
        return {"domain": "fx.local", "action": "items"}

    async def pick(intent, candidates):
        return {"endpoint_id": candidates[0]["endpoint_id"]}
    monkeypatch.setattr(pipeline, "parse_intent", parse)
    monkeypatch.setattr(pipeline, "pick_endpoint", pick)

    result = runner.invoke(main, ["run", "items", "--dry-run"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True and payload["plan"]["effect"] == "read"


def test_verify_unknown_exits_nonzero(runner):
    result = runner.invoke(main, ["verify", "nowhere"])
    assert result.exit_code == 1
    assert "error" in json.loads(result.stdout)


def test_show_outputs_json(runner):
    skill = SkillManifest(name="s", domain="s.com")
    save_skill(skill)
    result = runner.invoke(main, ["show", skill.skill_id])
    assert result.exit_code == 0 and json.loads(result.stdout)["domain"] == "s.com"


def test_auth_commands(runner):
    assert runner.invoke(main, ["auth", "set", "api.x.com", "k"]).exit_code == 0
    assert "api.x.com" in runner.invoke(main, ["auth", "list"]).output
    assert runner.invoke(main, ["auth", "remove", "api.x.com"]).exit_code == 0
    assert runner.invoke(main, ["auth", "remove", "api.x.com"]).exit_code == 1
