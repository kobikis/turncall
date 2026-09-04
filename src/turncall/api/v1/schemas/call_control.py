"""Live call control API schemas."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class EndCallRequest(BaseModel):
    reason: str = "api_request"


class BriefingSummary(BaseModel):
    """Briefing variant: auto-generate a summary from the transcript."""

    from_summary: bool = True


class TransferCallRequest(BaseModel):
    target_number: str = Field(..., pattern=r"^\+[1-9]\d{1,14}$")
    transfer_mode: str = Field(default="cold", pattern=r"^(warm|cold)$")
    # Spoken to the CALLER before the dial (both modes). Renames the old
    # non-functional pre_transfer_message. See ADR-0009.
    transfer_message: str | None = None
    # Spoken to the OPERATOR before bridging (warm only): a literal string, or
    # {"from_summary": true} to summarize the transcript on the fly.
    briefing: str | BriefingSummary | None = None
    # Spoken to the caller if the operator doesn't answer, then the call ends.
    fallback_message: str | None = None
    reason: str | None = None


class HandoffCallRequest(BaseModel):
    target_agent_id: UUID
    reason: str | None = None
    context_payload: dict[str, Any] | None = None


class SendDtmfRequest(BaseModel):
    digits: str = Field(..., pattern=r"^[0-9A-D*#wW]+$", max_length=32)


class InjectContextRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    role: str = Field(default="system", pattern=r"^(system|user|assistant)$")


class ControlResultResponse(BaseModel):
    success: bool
    message: str
    details: dict[str, Any] | None = None
