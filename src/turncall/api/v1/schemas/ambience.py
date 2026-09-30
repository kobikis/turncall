"""Ambience sound API schemas (ADR-0021)."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AmbienceSoundResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    name: str
    duration_ms: int
    rates: list[int]
    # rate -> a temporary URL for the rendition, on a single read: forced mono
    # at 8 kHz is a large audible change, and without this the only way to hear
    # what a caller hears is to place a call.
    rendition_urls: dict[str, str] | None = None

    @classmethod
    def from_row(
        cls, row: Any, rendition_urls: dict[str, str] | None = None
    ) -> "AmbienceSoundResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            duration_ms=row.duration_ms,
            rates=sorted(int(r) for r in row.renditions),
            rendition_urls=rendition_urls,
        )
