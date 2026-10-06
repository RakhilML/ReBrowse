"""Pydantic models for skills, endpoints, and execution traces."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from rebrowse.safety import Effect, classify_effect


def _id() -> str:
    return uuid4().hex[:12]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- Enums ---

class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class Idempotency(str, Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"


class SkillLifecycle(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


# --- Core Models ---

class ResponseSchema(BaseModel):
    type: str = "object"
    properties: dict[str, Any] | None = None
    items: Any | None = None
    required: list[str] | None = None
    inferred_from_samples: int = 0


class EndpointDescriptor(BaseModel):
    endpoint_id: str = Field(default_factory=_id)
    method: HttpMethod = HttpMethod.GET
    url_template: str
    description: str | None = None
    headers_template: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    path_params: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    idempotency: Idempotency = Idempotency.SAFE
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    reliability_score: float = 0.5
    response_schema: ResponseSchema | None = None
    trigger_url: str | None = None
    effect: Effect | None = None

    def get_effect(self) -> Effect:
        if self.effect is not None:
            return self.effect
        return classify_effect(self.method.value, self.url_template, self.body)


SKILL_SCHEMA_VERSION = 2


class SkillManifest(BaseModel):
    schema_version: int = 1  # records saved before versioning
    skill_id: str = Field(default_factory=_id)
    name: str
    domain: str
    description: str = ""
    intent_signature: str = ""
    endpoints: list[EndpointDescriptor] = Field(default_factory=list)
    lifecycle: SkillLifecycle = SkillLifecycle.ACTIVE
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# --- Capture Models ---

class RawRequest(BaseModel):
    url: str
    method: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str | None = None
    response_status: int = 0
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str | None = None
    timestamp: str = Field(default_factory=_now)


class CaptureResult(BaseModel):
    requests: list[RawRequest] = Field(default_factory=list)
    domain: str
    final_url: str
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    html: str | None = None
    js_bundles: dict[str, str] = Field(default_factory=dict)


# --- Execution Models ---

class ExecutionTrace(BaseModel):
    trace_id: str = Field(default_factory=_id)
    skill_id: str
    endpoint_id: str
    started_at: str = Field(default_factory=_now)
    completed_at: str | None = None
    success: bool = False
    status_code: int | None = None
    error: str | None = None
    result: Any | None = None
    truncated: bool = False
