"""Suppression list schemas (#227)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from turncall.api.v1.schemas import E164_PATTERN
from turncall.storage.models import SuppressedNumberRow


class SuppressNumberRequest(BaseModel):
    number: str = Field(..., pattern=E164_PATTERN)


class SuppressedNumberResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    number: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: SuppressedNumberRow) -> "SuppressedNumberResponse":
        return cls(id=row.id, number=row.e164_number, created_at=row.created_at)
