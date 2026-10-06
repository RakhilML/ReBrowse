"""MCP server exposing saved skills to agents."""

from __future__ import annotations

import io
import os
import sys
from typing import Any

from pydantic import BaseModel

from rebrowse.execution.executor import execute_endpoint
from rebrowse.models import EndpointDescriptor, SkillManifest
from rebrowse.safety import Effect


class SkillHit(BaseModel):
    skill_id: str
    domain: str
    operations: int
    score: float


class Operation(BaseModel):
    endpoint_id: str
    method: str
    url: str
    effect: str
    verification: str
    reliability: float
    description: str


class CallResult(BaseModel):
    endpoint: str
    effect: str
    success: bool = False
    status_code: int | None = None
    result: Any = None
    truncated: bool = False
    error: str | None = None
    confirmation_required: bool = False


def _find(skill_id: str, endpoint_id: str | None = None) -> tuple[SkillManifest, EndpointDescriptor | None]:
    from rebrowse.store.skills import list_all_skills
    skill = next((s for s in list_all_skills() if s.skill_id == skill_id), None)
    if skill is None:
        raise LookupError(f"skill {skill_id} not found")
    if endpoint_id is None:
        return skill, None
    ep = next((e for e in skill.endpoints if e.endpoint_id == endpoint_id), None)
    if ep is None:
        raise LookupError(f"endpoint {endpoint_id} not found in skill {skill_id}")
    return skill, ep


async def search(query: str, limit: int = 5) -> list[SkillHit]:
    from rebrowse.store.skills import search_skills
    return [
        SkillHit(skill_id=s.skill_id, domain=s.domain, operations=len(s.endpoints),
                 score=round(score, 3))
        for s, score in search_skills(query, limit=limit)
    ]


async def list_operations(skill_id: str) -> list[Operation]:
    skill, _ = _find(skill_id)
    return [
        Operation(endpoint_id=e.endpoint_id, method=e.method.value, url=e.url_template,
                  effect=e.get_effect().value, verification=e.verification_status.value,
                  reliability=e.reliability_score, description=e.description or "")
        for e in skill.endpoints
    ]


async def call(
    skill_id: str, endpoint_id: str, path_params: dict | None = None,
    query: dict | None = None, confirm: bool = False, read_only: bool = True,
) -> CallResult:
    skill, ep = _find(skill_id, endpoint_id)
    eff = ep.get_effect()
    label = f"{ep.method.value} {ep.url_template}"
    if read_only and eff is not Effect.READ:
        raise PermissionError(f"'{label}' is a {eff.value} operation; use 'act' with confirm=true")
    if eff is not Effect.READ and not confirm:
        return CallResult(
            endpoint=label, effect=eff.value, confirmation_required=True,
            error="This changes state on the site; call 'act' again with confirm=true.",
        )
    if query:
        ep = ep.model_copy(update={"query": {**ep.query, **{str(k): str(v) for k, v in query.items()}}})
    trace = await execute_endpoint(skill, ep, params=path_params, confirmed=True)
    return CallResult(
        endpoint=label, effect=eff.value, success=trace.success,
        status_code=trace.status_code, result=trace.result,
        truncated=trace.truncated, error=trace.error,
    )


def create_server():
    from fastmcp import FastMCP

    mcp = FastMCP("rebrowse")

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def search_skills(query: str, limit: int = 5) -> list[SkillHit]:
        """Find saved website skills by meaning."""
        return await search(query, limit)

    @mcp.tool(name="list_operations", annotations={"readOnlyHint": True, "openWorldHint": False})
    async def list_operations_tool(skill_id: str) -> list[Operation]:
        """List a skill's operations with effect (read/write/destructive) and health."""
        return await list_operations(skill_id)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def read(skill_id: str, endpoint_id: str,
                   path_params: dict | None = None, query: dict | None = None) -> CallResult:
        """Execute a READ operation and return its data."""
        return await call(skill_id, endpoint_id, path_params, query, read_only=True)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def act(skill_id: str, endpoint_id: str, confirm: bool = False,
                  path_params: dict | None = None, query: dict | None = None) -> CallResult:
        """Execute a write/destructive operation. Nothing is sent unless confirm=true."""
        return await call(skill_id, endpoint_id, path_params, query,
                          confirm=confirm, read_only=False)

    return mcp


# Windows: native imports (numpy, torch) touch fd 0 and deadlock against the stdio
# transport's blocking read, so the transport gets a duplicate and fd 0 becomes NUL.
def _isolate_stdin() -> None:
    pipe_fd = os.dup(0)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.close(null_fd)
    sys.stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(pipe_fd, "rb")), encoding="utf-8")


def main() -> None:
    if sys.platform == "win32":
        _isolate_stdin()
    create_server().run()


if __name__ == "__main__":
    main()
