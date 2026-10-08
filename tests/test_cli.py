from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from rebrowse.auth import vault
from rebrowse.cli import main
from rebrowse.llm import client as llm
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.orchestrator import pipeline
from rebrowse.store import skills as store
from rebrowse.store.skills import resolve_skill, save_skill

REPO = Path(__file__).resolve().parents[1]


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


def _saved_export_skill() -> SkillManifest:
    skill = SkillManifest(name="fx", domain="fx.local", endpoints=[
        EndpointDescriptor(method=HttpMethod.GET, url_template="http://fx.local/api/items"),
        EndpointDescriptor(method=HttpMethod.POST, url_template="http://fx.local/api/items"),
    ])
    save_skill(skill)
    return skill


def test_openapi_prints_document(runner):
    _saved_export_skill()
    result = runner.invoke(main, ["openapi", "fx.local"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["openapi"] == "3.1.0"


def test_openapi_writes_file(runner, tmp_path):
    skill = _saved_export_skill()
    out = tmp_path / "api.json"
    result = runner.invoke(main, ["openapi", skill.skill_id, "-o", str(out)])
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert (summary["paths"], summary["operations"]) == (1, 2)
    assert Path(summary["path"]) == out.resolve()
    text = out.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert json.loads(text)["info"]["x-rebrowse-skill-id"] == skill.skill_id


def test_openapi_unknown_exits_nonzero(runner):
    result = runner.invoke(main, ["openapi", "nowhere"])
    assert result.exit_code == 1
    assert "error" in json.loads(result.stdout)


def test_openapi_writes_utf8_without_touching_the_store(runner, tmp_path):
    skill = SkillManifest(name="Zoë's café", domain="cafe.local", endpoints=[
        EndpointDescriptor(url_template="http://cafe.local/api/menü")])
    save_skill(skill)
    stored = resolve_skill(skill.skill_id).model_dump()
    out = tmp_path / "api.json"

    assert runner.invoke(main, ["openapi", "cafe.local", "-o", str(out)]).exit_code == 0

    assert json.loads(out.read_text(encoding="utf-8"))["info"]["title"] == "Zoë's café"
    assert resolve_skill(skill.skill_id).model_dump() == stored


def test_openapi_unwritable_out_exits_nonzero(runner, tmp_path):
    _saved_export_skill()
    result = runner.invoke(main, ["openapi", "fx.local", "-o", str(tmp_path / "missing" / "api.json")])
    assert result.exit_code == 1
    assert "Could not write" in json.loads(result.stdout)["error"]


def test_openapi_stdout_matches_the_written_file(runner, tmp_path):
    _saved_export_skill()
    out = tmp_path / "api.json"

    printed = runner.invoke(main, ["openapi", "fx.local"]).stdout
    runner.invoke(main, ["openapi", "fx.local", "-o", str(out)])
    first = out.read_bytes()
    runner.invoke(main, ["openapi", "fx.local", "-o", str(out)])

    assert out.read_text(encoding="utf-8") == printed
    assert out.read_bytes() == first


def test_openapi_stays_offline_and_away_from_the_vault(runner, monkeypatch):
    _saved_export_skill()
    vault.store_cookies("fx.local", [{"name": "sid", "value": "cookie-secret", "domain": "fx.local"}])
    vault.store_api_key("fx.local", "key-secret")

    def boom(*args, **kwargs):
        raise AssertionError("export must not touch the network, LLM, embedder or vault")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(store, "_encode", boom)
    monkeypatch.setattr(vault, "_fernet", boom)

    result = runner.invoke(main, ["openapi", "fx.local"])

    assert result.exit_code == 0, result.output
    assert "cookie-secret" not in result.stdout and "key-secret" not in result.stdout


def test_group_help_lists_openapi(runner):
    assert ("openapi <target>  export a skill or a recording as an OpenAPI 3.1 document"
            in runner.invoke(main, ["--help"]).output)


def test_openapi_redirected_stdout_is_utf8_json(isolated):
    save_skill(SkillManifest(name="Zoë 日本 →", domain="uni.local", endpoints=[
        EndpointDescriptor(url_template="http://uni.local/api/menü", description="メニュー")]))
    env = {**os.environ, "REBROWSE_DATA_DIR": str(isolated)}

    proc = subprocess.run([sys.executable, "-m", "rebrowse", "openapi", "uni.local"],
                          capture_output=True, env=env, cwd=REPO, timeout=60, check=False)

    assert proc.returncode == 0, proc.stderr
    spec = json.loads(proc.stdout.decode("utf-8"))
    assert spec["info"]["title"] == "Zoë 日本 →"
    assert spec["paths"]["/api/menü"]["get"]["summary"] == "メニュー"
