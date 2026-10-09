"""Project API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class ProjectLimits(BaseModel):
    """A project's limits (#206). Null means the platform default (`LIMIT_*`),
    which is unlimited unless the deployment set one. A `PUT` replaces all of
    them, so a field left out goes back to that default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_calls: int | None = Field(
        default=None,
        ge=1,
        description="Live calls this project may hold at once, across every server",
    )
    outbound_calls_per_second: int | None = Field(
        default=None,
        ge=1,
        description="Outbound calls each of this project's numbers may start per second",
    )
    messages_per_minute: int | None = Field(
        default=None,
        ge=1,
        description="Inbound messages one text session may send per minute",
    )

    @classmethod
    def from_row(cls, row: Any) -> "ProjectLimits":
        return cls(
            live_calls=row.limit_live_calls,
            outbound_calls_per_second=row.limit_outbound_calls_per_second,
            messages_per_minute=row.limit_messages_per_minute,
        )


class ProjectResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    limits: ProjectLimits

    @classmethod
    def from_row(cls, row: Any) -> "ProjectResponse":
        return cls(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            limits=ProjectLimits.from_row(row),
        )
