from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rebrowse import mcp_server
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.safety import Effect
from rebrowse.store.skills import save_skill

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def skill(fixture_site):
    s = SkillManifest(name="fx", domain="127.0.0.1", endpoints=[
        EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/items",
                           effect=Effect.READ),
        EndpointDescriptor(method=HttpMethod.POST, url_template=f"{fixture_site}/api/echo",
                           body={"a": 1}, effect=Effect.WRITE),
    ])
    save_skill(s)
    return s


def _payload(result):
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content


async def test_tool_annotations_and_output_schema():
    from fastmcp import Client

    async with Client(mcp_server.create_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {"search_skills", "list_operations", "read", "act"}
    assert tools["search_skills"].annotations.readOnlyHint is True
    assert tools["read"].annotations.readOnlyHint is True
    assert tools["act"].annotations.readOnlyHint is False
    assert tools["act"].annotations.destructiveHint is True
    assert "success" in tools["read"].outputSchema["properties"]


async def test_read_refuses_write(skill):
    with pytest.raises(PermissionError):
        await mcp_server.call(skill.skill_id, skill.endpoints[1].endpoint_id, read_only=True)


async def test_act_requires_confirmation(skill, hits):
    out = await mcp_server.call(skill.skill_id, skill.endpoints[1].endpoint_id, read_only=False)
    assert out.confirmation_required and not out.success
    assert hits[("POST", "/api/echo")] == 0


async def test_act_executes_with_confirmation(skill, hits):
    out = await mcp_server.call(skill.skill_id, skill.endpoints[1].endpoint_id,
                                confirm=True, read_only=False)
    assert out.success and out.result["method"] == "POST"
    assert hits[("POST", "/api/echo")] == 1


async def test_read_executes(skill):
    out = await mcp_server.call(skill.skill_id, skill.endpoints[0].endpoint_id)
    assert out.success and "items" in out.result


async def test_unknown_ids_raise(skill):
    with pytest.raises(LookupError):
        await mcp_server.list_operations("missing")
    with pytest.raises(LookupError):
        await mcp_server.call(skill.skill_id, "missing")


async def test_list_operations_reports_effect_and_health(skill):
    ops = {o.url.rsplit("/", 1)[-1]: o for o in await mcp_server.list_operations(skill.skill_id)}
    assert ops["items"].effect == "read"
    assert ops["echo"].effect == "write"
    assert ops["items"].verification == "unverified"


async def test_search_finds_skill(skill):
    hits = await mcp_server.search("fx api items")
    assert hits and hits[0].skill_id == skill.skill_id


async def test_stdio_session_end_to_end(skill, isolated, hits):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env = {**os.environ, "REBROWSE_DATA_DIR": str(isolated), "REBROWSE_HOST_INTERVAL": "0"}
    transport = StdioTransport(command=sys.executable, args=["-m", "rebrowse", "mcp"],
                               env=env, cwd=str(REPO))
    read_id, write_id = skill.endpoints[0].endpoint_id, skill.endpoints[1].endpoint_id
    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
        ops = _payload(await client.call_tool("list_operations", {"skill_id": skill.skill_id}))
        read = _payload(await client.call_tool(
            "read", {"skill_id": skill.skill_id, "endpoint_id": read_id}))
        pending = _payload(await client.call_tool(
            "act", {"skill_id": skill.skill_id, "endpoint_id": write_id}))
        refused = await client.call_tool(
            "read", {"skill_id": skill.skill_id, "endpoint_id": write_id}, raise_on_error=False)

    assert names == {"search_skills", "list_operations", "read", "act"}
    assert {o["effect"] for o in ops} == {"read", "write"}
    assert read["success"] is True and "items" in read["result"]
    assert pending["confirmation_required"] is True
    assert refused.is_error
    assert hits[("POST", "/api/echo")] == 0
