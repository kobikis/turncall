"""Phone number API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BindPhoneNumberRequest(BaseModel):
    external_number_sid: str = Field(..., min_length=1, max_length=64)
    e164_number: str = Field(..., pattern=r"^\+[1-9]\d{1,14}$")
    routing_target_type: str = Field(..., pattern=r"^(agent|workflow|webhook)$")
    routing_target_id: UUID | None = None
    server_url: str | None = Field(default=None, max_length=2048)
    sms_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routing(self) -> "BindPhoneNumberRequest":
        if self.routing_target_type == "webhook":
            if not self.server_url:
                msg = "server_url is required when routing_target_type is 'webhook'"
                raise ValueError(msg)
        elif not self.routing_target_id:
            msg = "routing_target_id is required for agent/workflow routing"
            raise ValueError(msg)
        return self


class UpdatePhoneNumberRequest(BaseModel):
    """In-place update of a binding's routing/SMS config.

    The number's identity (SID, E.164) and its server_url_secret are stable —
    call-init endpoints keep verifying with the same secret across edits.
    """

    routing_target_type: str = Field(..., pattern=r"^(agent|workflow|webhook)$")
    routing_target_id: UUID | None = None
    server_url: str | None = Field(default=None, max_length=2048)
    sms_enabled: bool = False
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_routing(self) -> "UpdatePhoneNumberRequest":
        if self.routing_target_type == "webhook":
            if not self.server_url:
                msg = "server_url is required when routing_target_type is 'webhook'"
                raise ValueError(msg)
        elif not self.routing_target_id:
            msg = "routing_target_id is required for agent/workflow routing"
            raise ValueError(msg)
        return self


class RoutingWeightEntry(BaseModel):
    """A single entry in A/B routing weights."""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    weight: int = Field(..., ge=1, le=100)


class SetRoutingWeightsRequest(BaseModel):
    """Request to set A/B routing weights on a phone number."""

    weights: list[RoutingWeightEntry] = Field(..., min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "SetRoutingWeightsRequest":
        total = sum(w.weight for w in self.weights)
        if total != 100:
            msg = f"Weights must sum to 100 (got {total})"
            raise ValueError(msg)
        return self


class RoutingResponse(BaseModel):
    """Response for routing configuration."""

    model_config = ConfigDict(frozen=True)

    mode: str  # "single" or "weighted"
    routing_target_id: UUID | None = None
    weights: list[dict[str, Any]] | None = None


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    provider: str
    external_number_sid: str
    e164_number: str
    routing_target_type: str
    routing_target_id: UUID | None
    server_url: str | None
    server_url_secret: str | None  # HMAC secret for call-init signature verification
    sms_enabled: bool
    routing_weights: list[dict[str, Any]] | None
    metadata: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "PhoneNumberResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            provider=row.provider,
            external_number_sid=row.external_number_sid,
            e164_number=row.e164_number,
            routing_target_type=row.routing_target_type,
            routing_target_id=row.routing_target_id,
            server_url=row.server_url,
            server_url_secret=row.server_url_secret,
            sms_enabled=row.sms_enabled,
            routing_weights=row.routing_weights,
            metadata=row.metadata_json,
            created_at=row.created_at,
        )
