"""Call API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateOutboundCallRequest(BaseModel):
    to_number: str = Field(..., pattern=r"^\+[1-9]\d{1,14}$")
    from_number_id: UUID
    agent_id: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)


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

    @classmethod
    def from_row(cls, row: Any) -> "CallResponse":
        return cls(
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
            metadata=row.metadata_json,
            analysis=row.analysis_json,
            created_at=row.created_at,
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
    status: str  # "completed" | "pending" | "not_configured"
    analysis: dict[str, Any] | None = None
