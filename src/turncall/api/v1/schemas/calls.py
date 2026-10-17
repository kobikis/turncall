"""Call API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from turncall.api.v1.schemas import E164_PATTERN
from turncall.services.call_view import DYNAMIC_CONFIG, public_metadata


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
        return value


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
