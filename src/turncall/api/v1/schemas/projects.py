"""Project API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from turncall.services.redaction import DEFAULT_ENTITIES, SUPPORTED_ENTITIES


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


class ProjectRedaction(BaseModel):
    """A project's redaction (#313, ADR-0024): PII of these entity types is
    replaced in the transcript and every call event before it is stored or
    sent to a webhook. Never in what the agent hears during the call. A `PUT`
    replaces the whole setting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(
        default=False, description="Redact this project's calls from now on"
    )
    entities: list[str] | None = Field(
        default=None,
        description=(
            "Entity types to replace (Presidio's names, e.g. PHONE_NUMBER, "
            "EMAIL_ADDRESS, PERSON). Null = the default: " + ", ".join(DEFAULT_ENTITIES)
        ),
    )

    @field_validator("entities")
    @classmethod
    def _known_entities(cls, entities: list[str] | None) -> list[str] | None:
        if entities is None:
            return None
        if not entities:
            raise ValueError(
                "entities must name at least one type, or be null for the default"
            )
        unknown = sorted(set(entities) - SUPPORTED_ENTITIES)
        if unknown:
            raise ValueError(
                f"unknown entity types {unknown}; supported: "
                + ", ".join(sorted(SUPPORTED_ENTITIES))
            )
        return list(dict.fromkeys(entities))

    @classmethod
    def from_row(cls, row: Any) -> "ProjectRedaction":
        return cls(enabled=row.redaction_enabled, entities=row.redaction_entities)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    limits: ProjectLimits
    redaction: ProjectRedaction

    @classmethod
    def from_row(cls, row: Any) -> "ProjectResponse":
        return cls(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            limits=ProjectLimits.from_row(row),
            redaction=ProjectRedaction.from_row(row),
        )
