"""API key schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from turncall.domain.enums import ProjectRole


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    role: ProjectRole = ProjectRole.DEVELOPER
    environment: str | None = None


class ApiKeyResponse(BaseModel):
    """API key response (never includes the raw key or hash)."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    key_prefix: str
    name: str
    role: str
    environment: str | None
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreatedResponse(BaseModel):
    """Response after creating an API key (includes raw key ONCE)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    key_prefix: str
    raw_key: str
    name: str
    role: str
    environment: str | None
    created_at: datetime
