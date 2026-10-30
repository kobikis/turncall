"""Call API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from turncall.api.v1.schemas import E164_PATTERN
from turncall.services.call_view import (
    DYNAMIC_CONFIG,
    RESOLVED_SKILLS,
    public_metadata,
)


class CreateOutboundCallRequest(BaseModel):
    to_number: str = Field(..., pattern=E164_PATTERN)
    from_number_id: UUID
    agent_id: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _no_inline_agent(cls, value: dict[str, Any]) -> dict[str, Any]:
        # The media stream runs `metadata.dynamic_config` as the call's
        # config; only call-init may put one there (#276).
        if DYNAMIC_CONFIG in value:
            raise ValueError(
                f"metadata.{DYNAMIC_CONFIG} is reserved: the call runs agent_id"
            )
        if RESOLVED_SKILLS in value:
            # The record of what the call's skills resolved to (#425), which
            # the call's build writes and the read derives `skills` from.
            raise ValueError(f"metadata.{RESOLVED_SKILLS} is reserved")
        return value


class CallSkill(BaseModel):
    """One skill a call resolved at its start (#425)."""

    model_config = ConfigDict(frozen=True)

    skill: str
    version: int
    # The agent pinned this version rather than following the live one (#426).
    pinned: bool = False
    # Whether the call loaded it; null once the call's events expired (#316).
    loaded: bool | None


class CallProcedure(BaseModel):
    """What a call did of one procedure (#443), derived from its events."""

    model_config = ConfigDict(frozen=True)

    name: str
    # Every step reached, in order, a move back included.
    path: list[str]
    # The last step reached is an `end` step.
    completed: bool
    # The last step reached; null when the procedure never stood on one.
    ended_at: str | None
    # The last step reached, when it is an `escalate` step.
    escalated_at: str | None
    refused_moves: int


class CallResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    provider: str
    provider_call_sid: str | None
    direction: str
    from_number: str | None
    to_number: str | None
    active_agent_id: UUID | None
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    recording_status: str
    recording_url: str | None = None
    metadata: dict[str, Any]
    analysis: dict[str, Any] | None = None
    created_at: datetime
    # Derived, never stored — see services/call_view. Null when the reader
    # didn't load them (e.g. the outbound-create response, a call not yet over).
    ended_reason: str | None = None
    agent_name: str | None = None
    agent_version: int | None = None
    latency_summary: dict[str, Any] | None = None
    # `{role, from, to, error}` per provider failover, in order (#222).
    failovers: list[dict[str, Any]] = []
    # Every skill the call resolved, in the agent's order (#425); empty when
    # the agent has none.
    skills: list[CallSkill] = []
    # What the call did of each procedure, in the order each first moved
    # (#443); empty when it followed none, null once its events expired.
    procedures: list[CallProcedure] | None = []
    # When retention deleted each kind of the call's data (#316), null = kept.
    recording_expired_at: datetime | None = None
    transcript_expired_at: datetime | None = None
    events_expired_at: datetime | None = None

    @classmethod
    def from_row(
        cls, row: Any, derived: dict[str, Any] | None = None
    ) -> "CallResponse":
        return cls(
            **(derived or {}),
            id=row.id,
            project_id=row.project_id,
            provider=row.provider,
            provider_call_sid=row.provider_call_sid,
            direction=row.direction,
            from_number=row.from_number,
            to_number=row.to_number,
            active_agent_id=row.active_agent_id,
            status=row.status,
            started_at=row.started_at,
            ended_at=row.ended_at,
            duration_ms=row.duration_ms,
            recording_status=row.recording_status,
            recording_url=row.recording_url,
            metadata=public_metadata(row.metadata_json),
            analysis=row.analysis_json,
            created_at=row.created_at,
            recording_expired_at=getattr(row, "recording_expired_at", None),
            transcript_expired_at=getattr(row, "transcript_expired_at", None),
            events_expired_at=getattr(row, "events_expired_at", None),
        )


class CallEventResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    call_id: UUID
    event_type: str
    provider_timestamp: datetime | None
    internal_timestamp: datetime
    payload: dict[str, Any]
    sequence_number: int


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: UUID
    # "completed" | "pending" | "not_configured" | "expired" (deleted with the
    # transcript under the project's retention, #316)
    status: str
    analysis: dict[str, Any] | None = None
