"""Tool registration API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegisterToolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z_][a-z0-9_]*$")
    description: str = Field(..., min_length=1, max_length=1024)
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str = Field(default="sync", pattern=r"^(sync|async)$")
    webhook_url: str | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    max_retries: int = Field(default=1, ge=0, le=5)


class ToolResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters_schema: dict[str, Any]
    execution_mode: str
    webhook_url: str | None
    timeout_seconds: int
    max_retries: int


class ToolInvocationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    call_id: UUID
    tool_name: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None
    status: str
    latency_ms: int | None
    idempotency_key: str | None
    created_at: datetime
