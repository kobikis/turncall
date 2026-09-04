"""Webhook subscription API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateWebhookRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    events: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Event types to subscribe to, or ['*'] for all",
    )


class WebhookResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "WebhookResponse":
        event_list = (row.events or {}).get("events", ["*"])
        return cls(
            id=row.id,
            project_id=row.project_id,
            url=row.url,
            events=event_list,
            active=row.active,
            created_at=row.created_at,
        )
