"""Eval scenario and run API schemas (#68).

A scenario's `definition` is pipecat's own mapping and is validated by
round-tripping it through pipecat's parser: what a run will later build is
exactly what is checked here, so a definition that stores is one that can run.
The kind is computed from it eagerly and stored as a column, so every reader
switches on an explicit value rather than sniffing which key is present.
"""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from turncall.config import get_settings
from turncall.domain.enums import EvalKind, EvalModality, EvalToolPolicy
from turncall.evals.scenario import SCHEMA_VERSION, ScenarioError, validate
from turncall.services.tool_mocks import encode_mock

# A mock is keyed by tool name. Permissive on shape because an MCP server names
# its own tools and camelCase is common there, bounded because the key is
# matched against a tool name and nothing longer can be one.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# An agent advertising more tools than this does not exist; the cap is here so
# a mapping cannot be used to park unbounded JSON in the scenario row.
_MAX_MOCKS = 64


def _validated_mocks(mocks: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bound a `tool_mocks` mapping at the boundary.

    The value is stored verbatim and later handed to a model as a tool result,
    where it occupies the context for the rest of the conversation — so it is
    held to the same size limit a real tool result is (`TOOL_MAX_RESPONSE_BYTES`,
    which `tool_mocks.intercept` also applies at run time to rows stored before
    this check existed). Rejecting here is the better half of the pair: a
    truncated mock is a test that quietly means something else.
    """
    if not mocks:
        return mocks
    if len(mocks) > _MAX_MOCKS:
        raise ValueError(f"tool_mocks holds more than {_MAX_MOCKS} tools")
    limit = get_settings().tools.max_response_bytes
    for name, response in mocks.items():
        if not _TOOL_NAME.match(name):
            raise ValueError(f"tool_mocks key {name!r} is not a tool name")
        size = len(encode_mock(response).encode())
        if size > limit:
            raise ValueError(
                f"the mock for {name!r} is {size} bytes, over the "
                f"{limit}-byte tool result limit"
            )
    return mocks


def _validated_kind(definition: dict[str, Any], name: str) -> EvalKind:
    try:
        return validate(definition, name=name)
    except ScenarioError as exc:
        raise ValueError(str(exc)) from exc


class CreateEvalScenarioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any]
    # A tool name -> the canned result the model is handed instead of the call
    # being dispatched (#71). Under the default `mock_only` policy a tool with
    # no mock here ends the run rather than executing.
    tool_mocks: dict[str, Any] | None = None
    tool_policy: EvalToolPolicy | None = None
    tags: list[str] = Field(default_factory=list, max_length=32)
    default_target: dict[str, Any] | None = None

    @field_validator("tool_mocks")
    @classmethod
    def check_mocks(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validated_mocks(value)

    @model_validator(mode="after")
    def validate_definition(self) -> "CreateEvalScenarioRequest":
        _validated_kind(self.definition, self.name)
        return self

    @property
    def kind(self) -> EvalKind:
        return _validated_kind(self.definition, self.name)


class UpdateEvalScenarioRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any] | None = None
    tool_mocks: dict[str, Any] | None = None
    tool_policy: EvalToolPolicy | None = None
    tags: list[str] | None = Field(default=None, max_length=32)
    default_target: dict[str, Any] | None = None
    # The name keys nothing in a payload, but a run records `scenario_name` at
    # queue time, so renaming is allowed and old runs keep the old name.
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("tool_mocks")
    @classmethod
    def check_mocks(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validated_mocks(value)

    @model_validator(mode="after")
    def validate_definition(self) -> "UpdateEvalScenarioRequest":
        if self.definition is not None:
            _validated_kind(self.definition, self.name or "scenario")
        return self


class EvalScenarioResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    description: str | None
    kind: EvalKind
    definition: dict[str, Any]
    schema_version: str
    tool_mocks: dict[str, Any]
    tool_policy: EvalToolPolicy
    tags: list[str]
    default_target: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class EvalTarget(BaseModel):
    """What a run points at.

    Slice #70 accepts `agent` only; `agent_name` (latest published) and
    `inline` are #74, and are rejected at the boundary rather than accepted and
    then failed by the worker.
    """

    type: str = Field(..., pattern="^(agent|agent_name|inline)$")
    agent_id: UUID | None = None
    name: str | None = None
    agent: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "EvalTarget":
        if self.type != "agent":
            raise ValueError(
                f"target type {self.type!r} is not supported yet — use "
                "{'type': 'agent', 'agent_id': ...}"
            )
        if self.agent_id is None:
            raise ValueError("target type 'agent' needs an 'agent_id'")
        return self


class CreateEvalRunRequest(BaseModel):
    scenario_id: UUID
    target: EvalTarget
    modality: EvalModality = EvalModality.TEXT
    iterations: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_modality(self) -> "CreateEvalRunRequest":
        if self.modality is not EvalModality.TEXT:
            raise ValueError("audio modality is not supported yet — use 'text'")
        return self


class EvalRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    batch_id: UUID | None
    scenario_id: UUID | None
    scenario_name: str
    kind: EvalKind
    target: dict[str, Any]
    resolved_config: dict[str, Any]
    resolved_scenario: dict[str, Any]
    harness_config: dict[str, Any]
    agent_id: UUID | None
    modality: EvalModality
    iterations: int
    status: str
    passed_count: int
    failed_count: int
    results: list[dict[str, Any]]
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


__all__ = [
    "SCHEMA_VERSION",
    "CreateEvalRunRequest",
    "CreateEvalScenarioRequest",
    "EvalRunResponse",
    "EvalScenarioResponse",
    "EvalTarget",
    "UpdateEvalScenarioRequest",
]
