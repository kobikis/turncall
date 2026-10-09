"""Project credential request/response schemas (ADR-0020).

A write carries a value; no response here ever does.
"""

import base64
import binascii
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from turncall.api.errors import ApiError, ErrorCode
from turncall.api.v1.schemas.agents import AWSCredentialSetSchema
from turncall.domain.carriers import TELNYX
from turncall.domain.providers import AWS_ACCOUNT, CREDENTIAL_ACCOUNTS


class ApiKeyCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("api_key must not be blank")
        return value


class TelnyxCredential(ApiKeyCredential):
    """A Telnyx account: its API key and the public key it signs webhooks with.

    Both, because the project's own account is what sends its numbers'
    webhooks: without the key that signed them every one would be refused.
    """

    public_key: str = Field(min_length=1, max_length=256)

    @field_validator("public_key")
    @classmethod
    def an_ed25519_key(cls, value: str) -> str:
        value = value.strip()
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise ValueError(
                "public_key must be the account's base64 Ed25519 public key "
                "(32 bytes), as Mission Control shows it"
            )
        return value


class AWSCredential(AWSCredentialSetSchema):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def blank_is_absent(cls, value: Any) -> Any:
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def names_a_source(self) -> "AWSCredential":
        if not (self.access_key_id or self.role_arn or self.profile):
            raise ValueError(
                "an aws credential needs access_key_id + secret_access_key, "
                "role_arn, or profile — a region alone changes nothing"
            )
        return self


def _unprocessable(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=details,
    )


def require_known_provider(provider: str) -> None:
    if provider not in CREDENTIAL_ACCOUNTS:
        raise _unprocessable(
            f"Unknown provider {provider!r}. Credentials are held per provider "
            f"account: {', '.join(sorted(CREDENTIAL_ACCOUNTS))}"
        )


def parse_credential(provider: str, body: dict[str, Any]) -> dict[str, Any]:
    """Validate a write body for this provider into the document to store."""
    require_known_provider(provider)
    schemas = {AWS_ACCOUNT: AWSCredential, TELNYX: TelnyxCredential}
    schema = schemas.get(provider, ApiKeyCredential)
    try:
        parsed = schema.model_validate(body)
    except ValidationError as exc:
        messages = [e["msg"] for e in exc.errors()]
        raise _unprocessable(
            f"Invalid {provider} credential: {messages[0]}", {"errors": messages}
        ) from None
    return parsed.model_dump(exclude_none=True)


class CredentialResponse(BaseModel):
    """Presence only — the value is write-only."""

    provider: str
    present: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> "CredentialResponse":
        return cls(
            provider=row.provider,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
