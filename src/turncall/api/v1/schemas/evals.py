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
from turncall.domain.config_secrets import sanitize_config, sanitize_target
from turncall.domain.enums import (
    EvalKind,
    EvalModality,
    EvalRunStatus,
    EvalToolPolicy,
)
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
    # Derived, not stored (#95): a scenario whose every expectation is a bare
    # event parses cleanly and then reports `passed` forever against an agent
    # whose LLM returns nothing. Computed on every read rather than at create,
    # so an existing scenario that was trimmed into that state says so too.
    warnings: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_warnings(self) -> "EvalScenarioResponse":
        from turncall.evals.scenario import assertion_warnings

        if self.warnings:
            return self
        found = assertion_warnings(self.definition, name=self.name)
        return self.model_copy(update={"warnings": found}) if found else self

    @field_validator("default_target")
    @classmethod
    def _mask_default_target(cls, value: Any) -> Any:
        """An inline `default_target` carries a whole agent config (#74), and
        these endpoints are gated on `Auth` too — so the credentials a run
        response masks would otherwise walk out through the scenario."""
        return sanitize_target(value)


class ScenarioFromCallRequest(BaseModel):
    """Derive a scripted scenario from a call that already happened (#78)."""

    call_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=255)
    # False returns the draft for review; True stores it. The default is review
    # because the conversation is faithful but the expectations are not yet a
    # test, and a library of unedited drafts asserts whatever the agent did
    # that day, mistakes included.
    save: bool = False
    tags: list[str] = Field(default_factory=list, max_length=32)


class ScenarioFromSessionRequest(BaseModel):
    """Derive a scenario from a text conversation (#87).

    The same idea as `from-call` for SMS, the Chat API and WhatsApp text — and
    the source a manual test in a console produces, which had no route in.
    """

    session_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=255)
    save: bool = False
    tags: list[str] = Field(default_factory=list, max_length=32)


class ScenarioDraftResponse(BaseModel):
    """A draft, and a sentence saying that is what it is."""

    model_config = ConfigDict(frozen=True)

    name: str
    definition: dict[str, Any]
    tool_mocks: dict[str, Any]
    tool_policy: EvalToolPolicy
    default_target: dict[str, Any] | None
    schema_version: str
    note: str
    saved: bool
    scenario_id: UUID | None = None

    @field_validator("default_target")
    @classmethod
    def _mask_default_target(cls, value: Any) -> Any:
        """An inline `default_target` carries a whole agent config (#74), and
        these endpoints are gated on `Auth` too — so the credentials a run
        response masks would otherwise walk out through the scenario."""
        return sanitize_target(value)


class EvalTarget(BaseModel):
    """What a run points at (#74).

    Three forms, and the difference between the first two matters: an agent row
    is one immutable version, so `agent` pins a version forever — a scenario
    targeting it silently stops testing production the moment the next version
    is published. `agent_name` resolves to whatever is published at run time.
    `inline` has no row at all, which is how a prompt or model change is
    evaluated *before* publishing it, and the sandbox a scenario is pointed at
    when its tools have real side effects.
    """

    type: str = Field(..., pattern="^(agent|agent_name|inline)$")
    agent_id: UUID | None = None
    name: str | None = None
    environment: str | None = None
    agent: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "EvalTarget":
        if self.type == "agent":
            if self.agent_id is None:
                raise ValueError("target type 'agent' needs an 'agent_id'")
            return self
        if self.type == "agent_name":
            if not self.name:
                raise ValueError("target type 'agent_name' needs a 'name'")
            return self
        if not self.agent:
            raise ValueError("target type 'inline' needs an 'agent' configuration")
        # Validated exactly as an agent create is — same schema, same
        # `extra="forbid"`, so a mis-nested section is a 422 here rather than a
        # silently dropped field discovered when the run behaves oddly. The
        # worker checks again, but by then nobody is watching.
        from turncall.api.v1.schemas.agents import AgentConfigSchema

        try:
            AgentConfigSchema.model_validate(self.agent)
        except Exception as exc:
            raise ValueError(f"inline agent configuration is invalid: {exc}") from exc
        return self


class InlineScenario(BaseModel):
    """A scenario supplied on the run instead of stored first (#77).

    This is what a local file holds, and why the CLI needs no format of its
    own: the file is the API request body, validated by the same parser a
    stored scenario is. Nothing is written to `eval_scenarios` — the run's
    `scenario_id` is null and its `resolved_scenario` snapshot is the record,
    which is the same shape ADR-0017 uses for an inline agent.
    """

    name: str = Field(..., min_length=1, max_length=255)
    definition: dict[str, Any]
    tool_mocks: dict[str, Any] | None = None
    tool_policy: EvalToolPolicy | None = None

    @field_validator("tool_mocks")
    @classmethod
    def check_mocks(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validated_mocks(value)

    @model_validator(mode="after")
    def validate_definition(self) -> "InlineScenario":
        _validated_kind(self.definition, self.name)
        return self

    @property
    def kind(self) -> EvalKind:
        return _validated_kind(self.definition, self.name)


class CreateEvalRunRequest(BaseModel):
    """Run one stored scenario, every scenario carrying a tag (#75), or a
    scenario supplied inline (#77).

    Exactly one of the three. A tag fans out to one run per matching scenario,
    all sharing a batch id, so one request has one readable verdict — which is
    what the CLI's single exit code is built on.
    """

    scenario_id: UUID | None = None
    tag: str | None = Field(default=None, min_length=1, max_length=64)
    scenario: InlineScenario | None = None
    target: EvalTarget
    # `text` stays the default: it is the mode people run per PR — no STT, no
    # TTS, no local models, a fraction of the time. `audio` is the one that
    # covers what makes this a voice platform (#72).
    modality: EvalModality = EvalModality.TEXT
    iterations: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_selection(self) -> "CreateEvalRunRequest":
        chosen = sum(
            1 for v in (self.scenario_id, self.tag, self.scenario) if v is not None
        )
        if chosen != 1:
            raise ValueError("pass exactly one of 'scenario_id', 'tag' and 'scenario'")
        return self


class EvalBatchRunSummary(BaseModel):
    """One run inside a batch, without its results blob."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    scenario_id: UUID | None
    scenario_name: str
    status: EvalRunStatus
    passed_count: int
    failed_count: int
    iterations: int
    error: str | None
    # Carried here too (#96): this is the endpoint the CLI polls, and a warning
    # that only exists on the heavy per-run response is one nobody reads.
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class EvalBatchResponse(BaseModel):
    """A batch's outcome without fetching each run's transcripts (#75).

    `status` is the batch's verdict, derived the way a run derives its own from
    its iterations: anything still in flight makes it `running`, any failure
    fails it, and a batch where nothing reached a verdict is `errored` rather
    than passed — the same rule as a run, one level out.
    """

    model_config = ConfigDict(frozen=True)

    batch_id: UUID
    status: EvalRunStatus
    total: int
    counts: dict[str, int]
    passed_count: int
    failed_count: int
    runs: list[EvalBatchRunSummary]


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
    # Not verdicts, and not errors: things the scenario's author has to know
    # about the run they just got back (#96) — a mock that can never fire, an
    # agent whose real tools were allowed to. They were worker log lines, which
    # nobody reading a run ever sees.
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    # Masked on the way out, not at the call sites (#91): the run endpoints are
    # gated on `Auth`, so a viewer key reading a run would otherwise get every
    # credential in the agent's config in the clear. Validators catch every
    # `model_validate`, including rows written before the runner masked them.
    @field_validator("target")
    @classmethod
    def _mask_target(cls, value: Any) -> Any:
        return sanitize_target(value)

    @field_validator("resolved_config")
    @classmethod
    def _mask_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return sanitize_config(value)


__all__ = [
    "SCHEMA_VERSION",
    "CreateEvalRunRequest",
    "CreateEvalScenarioRequest",
    "EvalRunResponse",
    "EvalScenarioResponse",
    "EvalTarget",
    "UpdateEvalScenarioRequest",
]
