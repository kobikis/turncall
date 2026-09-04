"""Takeaway API schemas (ADR-0013)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _validate_json_schema(schema: dict[str, Any]) -> None:
    """Reject schemas the extractor can't validate against (fail at create,
    not silently after every call)."""
    import jsonschema

    jsonschema.validators.validator_for(schema).check_schema(schema)


class CreateTakeawayRequest(BaseModel):
    # Name keys the result in call.ended payloads — keep it identifier-shaped.
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str | None = Field(default=None, max_length=2000)
    schema_: dict[str, Any] = Field(..., alias="schema")
    prompt: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=100)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_schema(self) -> "CreateTakeawayRequest":
        try:
            _validate_json_schema(self.schema_)
        except Exception as exc:
            msg = f"schema is not a valid JSON Schema: {exc}"
            raise ValueError(msg) from exc
        return self


class UpdateTakeawayRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    prompt: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=100)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_schema(self) -> "UpdateTakeawayRequest":
        if self.schema_ is not None:
            try:
                _validate_json_schema(self.schema_)
            except Exception as exc:
                msg = f"schema is not a valid JSON Schema: {exc}"
                raise ValueError(msg) from exc
        return self


class TakeawayResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True, populate_by_name=True)

    id: UUID
    project_id: UUID
    name: str
    description: str | None
    schema_: dict[str, Any] = Field(alias="schema")
    prompt: str | None
    model: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "TakeawayResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            schema=row.schema,
            prompt=row.prompt,
            model=row.model,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
