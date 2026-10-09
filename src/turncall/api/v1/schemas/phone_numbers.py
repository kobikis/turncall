"""Phone number API schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from turncall.api.v1.schemas import E164_PATTERN
from turncall.domain.carriers import ASTERISK, CARRIERS, DEFAULT_CARRIER

Carrier = Literal[CARRIERS]  # type: ignore[valid-type]

# `provider` is the column's name and the field's old spelling; "provider"
# means an STT/LLM/TTS backend everywhere else in the API (ADR-0022).
_CARRIER_ALIASES = AliasChoices("carrier", "provider")
_CARRIER_DOC = (
    "The number's carrier. `provider` is accepted as a deprecated alias for it."
)


class CarrierField(BaseModel):
    """`carrier` on a write, with `provider` still accepted for it."""

    @model_validator(mode="before")
    @classmethod
    def one_carrier_spelling(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "carrier" in data
            and "provider" in data
            and data["carrier"] != data["provider"]
        ):
            msg = "carrier and provider (its deprecated alias) disagree; send carrier"
            raise ValueError(msg)
        return data


class TwilioAccountFields(BaseModel):
    """A number's own Twilio account. Both or neither: neither means the
    platform's TWILIO_* account. The token is write-only."""

    twilio_account_sid: str | None = Field(default=None, pattern=r"^AC[0-9a-fA-F]{32}$")
    twilio_auth_token: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_twilio_pair(self) -> "TwilioAccountFields":
        if bool(self.twilio_account_sid) != bool(self.twilio_auth_token):
            msg = "twilio_account_sid and twilio_auth_token must be set together"
            raise ValueError(msg)
        return self

    @property
    def has_own_twilio(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)


class BindPhoneNumberRequest(TwilioAccountFields, CarrierField):
    carrier: Carrier = Field(
        default=DEFAULT_CARRIER,
        validation_alias=_CARRIER_ALIASES,
        description=_CARRIER_DOC,
    )
    # Optional with the number's own account: TurnCall looks the SID up by
    # E.164 there, which is also what proves the credentials own the number.
    external_number_sid: str | None = Field(default=None, min_length=1, max_length=64)
    e164_number: str = Field(..., pattern=E164_PATTERN)
    routing_target_type: str = Field(..., pattern=r"^(agent|workflow|webhook)$")
    routing_target_id: UUID | None = None
    server_url: str | None = Field(default=None, max_length=2048)
    sms_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routing(self) -> "BindPhoneNumberRequest":
        if self.carrier == ASTERISK:
            # The PBX's dialplan routes the number; it has no SID to look up,
            # no Twilio account and no SMS (#209).
            if self.has_own_twilio or self.sms_enabled:
                msg = "an Asterisk number takes no Twilio account and no SMS"
                raise ValueError(msg)
        elif not self.external_number_sid and not self.has_own_twilio:
            msg = (
                "external_number_sid is required unless the number brings its own "
                "Twilio account (twilio_account_sid + twilio_auth_token)"
            )
            raise ValueError(msg)
        if self.routing_target_type == "webhook":
            if not self.server_url:
                msg = "server_url is required when routing_target_type is 'webhook'"
                raise ValueError(msg)
        elif not self.routing_target_id:
            msg = "routing_target_id is required for agent/workflow routing"
            raise ValueError(msg)
        return self


class UpdatePhoneNumberRequest(TwilioAccountFields, CarrierField):
    """In-place update of a binding's routing/SMS config.

    The number's identity (carrier, SID, E.164) and its server_url_secret are
    stable — call-init endpoints keep verifying with the same secret across
    edits. `carrier` may be sent but must be the number's own (null = it).
    """

    carrier: Carrier | None = Field(
        default=None, validation_alias=_CARRIER_ALIASES, description=_CARRIER_DOC
    )

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
    carrier: str
    provider: str = Field(deprecated="Use `carrier`; this is the same value.")
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
    # The number's own Twilio account, or null for the platform's. Never the token.
    twilio_account_sid: str | None = None
    twilio_credentials: str = "platform"  # "number" | "platform"

    @classmethod
    def from_row(cls, row: Any) -> "PhoneNumberResponse":
        own = bool(getattr(row, "twilio_auth_token_ciphertext", None))
        return cls(
            id=row.id,
            project_id=row.project_id,
            carrier=row.provider,
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
            twilio_account_sid=getattr(row, "twilio_account_sid", None)
            if own
            else None,
            twilio_credentials="number" if own else "platform",
        )
