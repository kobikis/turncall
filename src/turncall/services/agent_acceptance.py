"""Agent acceptance: whether an agent config can run, decided once (#474).

`accept_agent` takes a raw config mapping or an agent id, the project the
work runs in and the **origin** — where the config came from — and answers an
`AcceptedAgent` (the parsed domain config, the agent id or None for an inline
agent, and warnings) or an `AgentRefusal` (a reason and the field it names).
It never raises for a config that cannot run; only for the database, and for
an origin no slice has wired yet.

The origin decides only what is refused and what is warned about, and that
choice is `POLICIES`, one row per `AgentOrigin`. The rules themselves are not
rewritten here: each keeps its own function and message
(`xai_config.llm_refusal` / `tts_refusal`, `grok_voice.refusal`,
`deepgram_flux.mixes_flux`, `eleven_realtime`'s, the domain
`procedures_refusal`), so the API and this module say the same words.

`call_init` (#476) is the domain model's parse plus the rules the build holds
for an inline agent, applied where the build would meet them — an S2S rule
only on an S2S agent, the TTS's only on cascade — so nothing that builds
today is refused (#474 story 29). The full API schema is not run on it: its
allowlists and required models refuse configs that run. The build keeps its
copies until #481.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.models import AgentConfig, resolve_delegation
from turncall.domain.procedure_graph import procedures_refusal
from turncall.orchestrator import eleven_realtime, grok_voice, xai_config
from turncall.orchestrator.config_warnings import (
    knowledge_mode_warning,
    procedures_on_s2s_warning,
    skills_on_s2s_warning,
)
from turncall.orchestrator.deepgram_flux import MIXED_FLUX_GROUP, mixes_flux
from turncall.storage.repositories import agent_repo, knowledge_repo


class AgentOrigin(StrEnum):
    """Where an agent config came from. Closed: a new entry point is a new row."""

    API = "api"  # agent create and update
    CALL_INIT = "call_init"  # a call-init answer, on any transport
    HANDOFF = "handoff"  # a handoff's target
    EVAL_TARGET = "eval_target"  # an eval run's `agent` / `agent_name` target
    EVAL_INLINE = "eval_inline"  # an eval run's `inline` target
    DEMO = "demo"  # the website demo's agents file
    STORED_CALL = "stored_call"  # a number routed to a stored agent


class UnknownFields(StrEnum):
    """What a top-level key the config schema does not know becomes."""

    REFUSE = "refuse"
    WARN = "warn"  # warned about, then dropped by the domain parse


@dataclass(frozen=True)
class OriginPolicy:
    """One origin's refuse-or-warn choices.

    Provider rules and the procedures graph rule are refused on every origin,
    and an agent id must be one of the project's wherever one is taken; those
    are not choices, so they are not columns.
    """

    unknown_fields: UnknownFields
    # Plain `http://` outbound URLs (#340): refused at the API, allowed on
    # every inline config. A stored config was checked when it was saved.
    plain_http: bool
    # Whether the source may be an agent id rather than a raw config.
    takes_agent_id: bool
    # The issue that wires the origin, or None once it is wired.
    wired_by: str | None
    # Run the API's whole `AgentConfigSchema` (its allowlists, required
    # models, strict nested sections): agent create and update (#478), and
    # `eval_inline` and `demo`, as each did before acceptance (#479). A
    # refusal keeps the schema's own errors (`schema_errors`), which the
    # agent endpoints answer as their 422, and the config that runs is the
    # one a save would store.
    full_schema: bool = False
    # Whether `knowledge_bases` may name the config's KBs. Not on a config
    # saved as an agent row, whose KBs are its links — one source, never two
    # that can disagree (#260). Checked with `full_schema` only.
    inline_knowledge_bases: bool = True


POLICIES: Mapping[AgentOrigin, OriginPolicy] = {
    AgentOrigin.API: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=False,
        takes_agent_id=False,
        wired_by=None,
        full_schema=True,
        inline_knowledge_bases=False,
    ),
    AgentOrigin.CALL_INIT: OriginPolicy(
        UnknownFields.WARN, plain_http=True, takes_agent_id=True, wired_by=None
    ),
    # A stored config was written through the schema, so an unknown key there
    # is one a later release removed: nothing to refuse the call over.
    AgentOrigin.HANDOFF: OriginPolicy(
        UnknownFields.WARN, plain_http=True, takes_agent_id=True, wired_by="#480"
    ),
    # A stored config, like a handoff's; the eval looks the row up itself.
    AgentOrigin.EVAL_TARGET: OriginPolicy(
        UnknownFields.WARN, plain_http=True, takes_agent_id=True, wired_by=None
    ),
    AgentOrigin.EVAL_INLINE: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=True,
        takes_agent_id=False,
        wired_by=None,
        full_schema=True,
    ),
    AgentOrigin.DEMO: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=True,
        takes_agent_id=False,
        wired_by=None,
        full_schema=True,
    ),
    AgentOrigin.STORED_CALL: OriginPolicy(
        UnknownFields.WARN, plain_http=True, takes_agent_id=True, wired_by="#480"
    ),
}

UNKNOWN_FIELD = "unknown_field"


@dataclass(frozen=True)
class AgentWarning:
    """Something an accepted agent's call ignores. `field` when it names one."""

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class AcceptedAgent:
    """A config that can run. `agent_id` is None for an inline agent."""

    config: AgentConfig
    agent_id: UUID | None
    warnings: tuple[AgentWarning, ...] = ()
    # The config as the API stores it (the schema's dump); `full_schema` only.
    stored: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AgentRefusal:
    """Why a config cannot run, and the field it names (None: the whole)."""

    reason: str
    field: str | None = None
    # The schema's own errors, whole (`full_schema` only): the agent endpoints
    # answer them as the request validation error they always were.
    schema_errors: tuple[Mapping[str, Any], ...] = ()

    def describe(self) -> str:
        """The reason with the field it names in front, once."""
        if self.field is None or self.reason.startswith(self.field):
            return self.reason
        return f"{self.field}: {self.reason}"


AgentSource = Mapping[str, Any] | UUID


async def accept_agent(
    source: AgentSource,
    *,
    project_id: UUID,
    origin: AgentOrigin,
    session: AsyncSession | None,
) -> AcceptedAgent | AgentRefusal:
    """Whether `source` can run in `project_id`, coming from `origin`.

    An agent id resolves only within the project: another project's id is
    refused exactly as one naming no agent, and its config is never read.
    `session` may be None for a raw config.
    """
    policy = _wired(origin)
    if isinstance(source, UUID):
        if not policy.takes_agent_id or session is None:
            raise TypeError(f"origin {origin.value!r} takes no agent id here")
        return await _accept_stored(session, source, project_id=project_id)
    return accept_raw_config(source, origin=origin)


def accept_raw_config(
    raw: Mapping[str, Any], *, origin: AgentOrigin
) -> AcceptedAgent | AgentRefusal:
    """`accept_agent` for a raw config, which needs no project, database or
    event loop — for a caller with none, like the demo loading its file."""
    policy = _wired(origin)
    if policy.full_schema:
        return _accept_schema(raw, policy)
    return _accept_config(
        raw, agent_id=None, unknown_fields=policy.unknown_fields, kb_modes=None
    )


def _wired(origin: AgentOrigin) -> OriginPolicy:
    policy = POLICIES[origin]
    if policy.wired_by is not None:
        raise NotImplementedError(
            f"agent acceptance for origin {origin.value!r} lands with {policy.wired_by}"
        )
    return policy


async def _accept_stored(
    session: AsyncSession, agent_id: UUID, *, project_id: UUID
) -> AcceptedAgent | AgentRefusal:
    row = await agent_repo.get_agent_by_id(session, agent_id, project_id=project_id)
    if row is None:
        return AgentRefusal(
            f"agent {agent_id} is not an agent of this project", field="agent_id"
        )
    modes = await knowledge_repo.kb_modes_by_agent(session, [row.id])
    # A stored blob's keys are not checked: the schema wrote them.
    return _accept_config(
        row.config_blob,
        agent_id=row.id,
        unknown_fields=None,
        kb_modes=modes.get(row.id, []),
    )


def _accept_config(
    raw: Any,
    *,
    agent_id: UUID | None,
    unknown_fields: UnknownFields | None,
    kb_modes: list[str] | None,
) -> AcceptedAgent | AgentRefusal:
    """`unknown_fields` None: the keys are not checked. `kb_modes` None: an
    inline config, whose own `knowledge_bases` name them."""
    if not isinstance(raw, Mapping):
        return AgentRefusal("the agent config must be a JSON object")
    unknown = _unknown_fields(raw) if unknown_fields is not None else []
    if unknown and unknown_fields is UnknownFields.REFUSE:
        return AgentRefusal(f"{unknown[0]} is not an agent config field", unknown[0])
    try:
        config = AgentConfig.model_validate(dict(raw))
    except ValidationError as invalid:
        return _unreadable(invalid)
    if (refusal := _first_rule_refusal(config)) is not None:
        return refusal
    modes = (
        [str(kb.mode) for kb in config.knowledge_bases]
        if kb_modes is None
        else kb_modes
    )
    warnings = (
        *(_unknown_field_warning(name) for name in unknown),
        *_s2s_warnings(config, modes),
    )
    return AcceptedAgent(config=config, agent_id=agent_id, warnings=warnings)


def _accept_schema(raw: Any, policy: OriginPolicy) -> AcceptedAgent | AgentRefusal:
    """`raw` through the API's whole schema, then the domain parse and the
    rules as on every origin (the schema holds them too, so they refuse
    nothing more). Plain http is checked as `plain_http_allowed` decides
    unless the origin allows it; the dump leaves inline knowledge bases out
    (`exclude=True`), so an origin that takes them gets them put back."""
    from turncall.api.v1.schemas import INLINE_CONFIG
    from turncall.api.v1.schemas.agents import AgentConfigSchema

    try:
        # `from_attributes` as FastAPI's body parsing validated it, so the
        # errors read as they did there.
        checked = AgentConfigSchema.model_validate(
            raw,
            from_attributes=True,
            context=INLINE_CONFIG if policy.plain_http else None,
        )
    except ValidationError as invalid:
        errors = tuple(invalid.errors(include_url=False))
        return replace(_schema_refusal(raw, invalid), schema_errors=errors)
    if checked.knowledge_bases and not policy.inline_knowledge_bases:
        return AgentRefusal(INLINE_KNOWLEDGE_BASES, "knowledge_bases")
    stored = checked.model_dump()
    inline_kbs = [kb.model_dump() for kb in checked.knowledge_bases]
    try:
        config = AgentConfig.model_validate({**stored, "knowledge_bases": inline_kbs})
    except ValidationError as invalid:
        return _unreadable(invalid)
    if (refusal := _first_rule_refusal(config)) is not None:
        return refusal
    modes = [str(kb.mode) for kb in config.knowledge_bases]
    warnings = tuple(_s2s_warnings(config, modes))
    return AcceptedAgent(config=config, agent_id=None, warnings=warnings, stored=stored)


def _schema_refusal(raw: Any, invalid: ValidationError) -> AgentRefusal:
    """A schema refusal in the words the other origins use: a config that is
    not an object, then its first unknown field, then the schema's first
    error."""
    if not isinstance(raw, Mapping):
        return AgentRefusal("the agent config must be a JSON object")
    if unknown := _unknown_fields(raw):
        return AgentRefusal(f"{unknown[0]} is not an agent config field", unknown[0])
    return _unreadable(invalid)


INLINE_KNOWLEDGE_BASES = (
    "knowledge_bases is for an inline agent only; link a knowledge base "
    "to a stored agent with POST /v1/agents/{agent_id}/knowledge-bases"
)


def _unknown_fields(raw: Mapping[str, Any]) -> list[str]:
    """Top-level keys the config does not have. Nested ones are invisible
    here as at the API, whose nested models do not forbid extras either."""
    return sorted(str(key) for key in raw if key not in AgentConfig.model_fields)


def _unknown_field_warning(name: str) -> AgentWarning:
    return AgentWarning(
        UNKNOWN_FIELD,
        f"{name} is not an agent config field; it was ignored",
        field=name,
    )


def _unreadable(invalid: ValidationError) -> AgentRefusal:
    error = invalid.errors()[0]
    field = _path(error["loc"])
    # A validator's ValueError reads "Value error, <its message>".
    message = error["msg"].removeprefix("Value error, ")
    return AgentRefusal(f"{field}: {message}" if field else message, field or None)


def _path(loc: tuple[Any, ...]) -> str:
    """`("llm", "failover", 0, "temperature")` as `llm.failover[0].temperature`."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


# --- the rules ------------------------------------------------------------------

Rule = Callable[[AgentConfig], Iterator[AgentRefusal]]


def _first_rule_refusal(config: AgentConfig) -> AgentRefusal | None:
    for rule in _RULES:
        for refusal in rule(config):
            return refusal
    return None


def _builds_an_llm(config: AgentConfig) -> bool:
    """Cascade builds the agent's `llm`; so does gpt-live-1 for a client
    delegate (#244). No other S2S agent builds it."""
    if config.pipeline_mode != "s2s":
        return True
    has_tools = bool(
        config.tools
        or config.mcp_servers
        or any(str(kb.mode) == "tool" for kb in config.knowledge_bases)
    )
    delegation = resolve_delegation(config.s2s, has_tools=has_tools)
    return delegation is not None and delegation.mode == "client"


def _xai_llm(config: AgentConfig) -> Iterator[AgentRefusal]:
    if not _builds_an_llm(config):
        return
    members = [("llm", config.llm)] + [
        (f"llm.failover[{i}]", entry) for i, entry in enumerate(config.llm.failover)
    ]
    for field, llm in members:
        if llm.provider == "xai" and (problem := xai_config.llm_refusal(llm)):
            yield AgentRefusal(problem, field)


def _xai_tts(config: AgentConfig) -> Iterator[AgentRefusal]:
    if config.pipeline_mode == "s2s":
        return
    members = [("tts", config.tts)] + [
        (f"tts.failover[{i}]", entry) for i, entry in enumerate(config.tts.failover)
    ]
    for field, tts in members:
        if tts.provider == "xai" and (problem := xai_config.tts_refusal(tts)):
            yield AgentRefusal(problem, field)


def _flux_group(config: AgentConfig) -> Iterator[AgentRefusal]:
    if config.pipeline_mode != "s2s" and mixes_flux(config.stt):
        yield AgentRefusal(MIXED_FLUX_GROUP, "stt.failover")


def _realtime(config: AgentConfig) -> Iterator[AgentRefusal]:
    if config.pipeline_mode != "s2s":
        return
    s2s = config.s2s
    if s2s.provider == "xai" and (problem := grok_voice.refusal(s2s)):
        yield AgentRefusal(problem, "s2s")
    if s2s.provider == "elevenlabs":
        if problem := eleven_realtime.refusal(s2s):
            yield AgentRefusal(problem, "s2s")
        if problem := eleven_realtime.duration_refusal(
            config.max_call_duration_seconds
        ):
            yield AgentRefusal(problem, "max_call_duration_seconds")
    if problem := eleven_realtime.gateway_refusal(s2s):
        yield AgentRefusal(problem, "s2s")


def _procedures(config: AgentConfig) -> Iterator[AgentRefusal]:
    """The domain graph rule, as the build applies it: no step-tool check
    (#447 is the API's), and none on S2S, which follows no procedure."""
    if config.pipeline_mode == "s2s":
        return
    if (problem := procedures_refusal(config.procedures)) is not None:
        yield AgentRefusal(problem, "procedures")


_RULES: tuple[Rule, ...] = (_flux_group, _xai_llm, _xai_tts, _realtime, _procedures)


def _s2s_warnings(config: AgentConfig, kb_modes: list[str]) -> Iterator[AgentWarning]:
    """What an S2S agent's call ignores. The build logs these itself
    (`skills_skipped`, `procedures_skipped`, the KB drop), so callers don't."""
    for warning in (
        skills_on_s2s_warning(config),
        procedures_on_s2s_warning(config),
        knowledge_mode_warning(config, kb_modes),
    ):
        if warning is not None:
            yield AgentWarning(warning["code"], warning["message"])
