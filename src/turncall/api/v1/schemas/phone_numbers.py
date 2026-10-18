"""Phone number API schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from turncall.api.v1.schemas import E164_PATTERN
from turncall.domain.carriers import (
    ASTERISK,
    CARRIERS,
    DEFAULT_CARRIER,
    NUMBER_TYPES,
    PURCHASE_CARRIERS,
    TELNYX,
    TWILIO,
)

Carrier = Literal[CARRIERS]  # type: ignore[valid-type]
# A carrier TurnCall searches and buys numbers on (ADR-0025).
PurchaseCarrier = Literal[PURCHASE_CARRIERS]  # type: ignore[valid-type]
NumberType = Literal[NUMBER_TYPES]  # type: ignore[valid-type]

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


def only_on_twilio(body: TwilioAccountFields, carrier: str) -> None:
    """A number's own Twilio account and SMS are Twilio features (ADR-0022:
    SMS on Telnyx numbers is out of scope); on another carrier they would be
    stored and never used."""
    if body.has_own_twilio:
        msg = (
            "twilio_account_sid/twilio_auth_token apply to Twilio numbers, "
            f"not {carrier}"
        )
        raise ValueError(msg)
    if getattr(body, "sms_enabled", False):
        msg = f"sms_enabled is not supported on {carrier} numbers"
        raise ValueError(msg)


class BindPhoneNumberRequest(TwilioAccountFields, CarrierField):
    carrier: Carrier = Field(
        default=DEFAULT_CARRIER,
        validation_alias=_CARRIER_ALIASES,
        description=_CARRIER_DOC,
    )
    # Optional with the number's own Twilio account, and on Telnyx: TurnCall
    # looks the SID up by E.164 on the account, which is also what proves the
    # account owns the number.
    external_number_sid: str | None = Field(default=None, min_length=1, max_length=64)
    e164_number: str = Field(..., pattern=E164_PATTERN)
    routing_target_type: str = Field(..., pattern=r"^(agent|workflow|webhook)$")
    routing_target_id: UUID | None = None
    server_url: str | None = Field(default=None, max_length=2048)
    sms_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routing(self) -> "BindPhoneNumberRequest":
        if self.carrier != TWILIO:
            only_on_twilio(self, self.carrier)
        # Telnyx is looked up by its number on the account (#219); the PBX's
        # dialplan routes an Asterisk number, which has no SID at all (#209).
        looked_up = self.has_own_twilio or self.carrier in (TELNYX, ASTERISK)
        if not self.external_number_sid and not looked_up:
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


# --- search and buy (#319, ADR-0025) ---------------------------------------


class NumberCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    voice: bool
    sms: bool
    mms: bool


class AvailableNumberResponse(BaseModel):
    """A number a carrier account can buy. The same shape on every carrier;
    what a carrier does not say is null."""

    model_config = ConfigDict(frozen=True)

    carrier: str
    e164_number: str
    friendly_name: str | None = None
    country: str | None = None
    region: str | None = None
    locality: str | None = None
    postal_code: str | None = None
    number_type: str
    capabilities: NumberCapabilities


class BuyPhoneNumberRequest(TwilioAccountFields):
    """Buy a number a search returned. On Twilio, the account it is bought on
    is the pair given here, else the platform's TWILIO_* account; bind the
    number with the same pair."""

    carrier: PurchaseCarrier = Field(
        default=DEFAULT_CARRIER,
        description="The carrier whose account the number is bought on.",
    )
    e164_number: str = Field(..., pattern=E164_PATTERN)

    @model_validator(mode="after")
    def validate_account(self) -> "BuyPhoneNumberRequest":
        if self.carrier != TWILIO:
            only_on_twilio(self, self.carrier)
        return self


class PurchasedNumberResponse(BaseModel):
    """A bought number, and what binding it needs: send `carrier`,
    `e164_number` and `external_number_sid` to `POST /v1/phone-numbers`
    (with the same Twilio pair, when `twilio_credentials` is "number").
    `status` is "active" once the number can be bound."""

    model_config = ConfigDict(frozen=True)

    carrier: str
    e164_number: str
    external_number_sid: str | None
    friendly_name: str | None = None
    status: str = "active"
    # Twilio only: the account it was bought on, null for the platform's.
    twilio_account_sid: str | None = None
    twilio_credentials: str | None = None  # "number" | "platform"; null off Twilio
