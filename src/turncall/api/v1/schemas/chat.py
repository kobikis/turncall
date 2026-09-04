"""Chat API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SendChatMessageRequest(BaseModel):
    session_id: UUID | None = None
    previous_chat_id: UUID | None = None
    agent_id: UUID
    message: str = Field(..., min_length=1, max_length=4096)
    channel: str = Field(default="api", pattern=r"^(sms|web|api)$")
    customer_number: str | None = None
    turncall_number: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_context(self) -> "SendChatMessageRequest":
        if self.session_id and self.previous_chat_id:
            msg = "Cannot provide both session_id and previous_chat_id"
            raise ValueError(msg)
        return self


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    session_id: UUID
    role: str
    content: str
    channel: str
    token_count: int | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "ChatMessageResponse":
        return cls(
            id=row.id,
            session_id=row.session_id,
            role=row.role,
            content=row.content,
            channel=row.channel,
            token_count=row.token_count,
            created_at=row.created_at,
        )


class ChatSessionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    agent_id: UUID
    customer_number: str
    turncall_number: str
    status: str
    channel: str
    message_count: int
    last_activity_at: datetime
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "ChatSessionResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            agent_id=row.agent_id,
            customer_number=row.customer_number,
            turncall_number=row.turncall_number,
            status=row.status,
            channel=row.channel,
            message_count=row.message_count,
            last_activity_at=row.last_activity_at,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
