"""Audit log read schemas (#317, ADR-0024)."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditEntryResponse(BaseModel):
    """One change made through the API."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID = Field(description="The entry's id")
    project_id: UUID
    api_key_id: UUID | None = Field(
        description="The API key that made the change; null when the platform "
        "credential did (`actor: platform`)"
    )
    actor: str = Field(description="`api_key` or `platform`")
    resource_type: str
    resource_id: str = Field(
        description="The resource's id; a credential's is its provider account"
    )
    action: str
    changes: dict[str, Any] = Field(
        description='`{"/field/path": {"before": ..., "after": ...}}` for each '
        "field that changed, as the API reads it, secrets masked as `***`. "
        "Each key is an RFC 6901 JSON Pointer: `~` and `/` in a field's name "
        "read `~0` and `~1`"
    )
    request_id: UUID = Field(
        description="The request that made the change, shared by every entry "
        "it wrote: the id of its first entry, the one `X-TurnCall-Audit-Id` "
        "named on its response. Filter by it to read the whole request"
    )
    created_at: datetime


def encode_cursor(created_at: datetime, entry_id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(entry_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """The (created_at, id) a cursor names; ValueError if it names nothing."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, entry_id = json.loads(raw)
        return datetime.fromisoformat(at), UUID(entry_id)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError("not a cursor this API returned") from exc
