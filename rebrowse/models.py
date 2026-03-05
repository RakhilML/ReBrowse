"""Pydantic models for skills, endpoints, and execution traces."""

from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
from uuid import uuid4


def _id() -> str:
    return uuid4().hex[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    properties: Optional[dict[str, Any]] = None
    items: Optional[Any] = None
    required: Optional[list[str]] = None
    inferred_from_samples: int = 0


class EndpointDescriptor(BaseModel):
    endpoint_id: str = Field(default_factory=_id)
    method: HttpMethod = HttpMethod.GET
    url_template: str
    description: Optional[str] = None
    headers_template: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    path_params: dict[str, str] = Field(default_factory=dict)
    body: Optional[Any] = None
    idempotency: Idempotency = Idempotency.SAFE
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    reliability_score: float = 0.5
    response_schema: Optional[ResponseSchema] = None
    trigger_url: Optional[str] = None
    dom_extraction: bool = False


class SkillManifest(BaseModel):
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
    request_body: Optional[str] = None
    response_status: int = 0
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Optional[str] = None
    timestamp: str = Field(default_factory=_now)


class CaptureResult(BaseModel):
    requests: list[RawRequest] = Field(default_factory=list)
    domain: str
    final_url: str
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    html: Optional[str] = None
    js_bundles: dict[str, str] = Field(default_factory=dict)


# --- Execution Models ---

class ExecutionTrace(BaseModel):
    trace_id: str = Field(default_factory=_id)
    skill_id: str
    endpoint_id: str
    started_at: str = Field(default_factory=_now)
    completed_at: Optional[str] = None
    success: bool = False
    status_code: Optional[int] = None
    error: Optional[str] = None
    result: Optional[Any] = None


class OrchestratorResult(BaseModel):
    result: Any = None
    source: str = "live-capture"  # "store" | "live-capture" | "dom-fallback"
    skill: Optional[SkillManifest] = None
    trace: Optional[ExecutionTrace] = None
    timing_ms: float = 0.0
