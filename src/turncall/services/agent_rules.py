"""The rules agent acceptance applies to a parsed config (#474).

What a refusal and a warning are, and the rules that produce them. The rules
are not rewritten here: each keeps its own function and message
(`xai_config.llm_refusal` / `tts_refusal`, `grok_voice.refusal`,
`deepgram_flux.mixes_flux`, `eleven_realtime`'s, the domain
`procedures_refusal`), so the API and acceptance say the same words. Every
origin applies all of them; `services/agent_acceptance` decides the rest.

The agent's `llm` rules apply only where an `llm` is built: on cascade, and
on gpt-live-1 (`openai_live`) when it gets a client delegate, which it does
when it has tools (#244). The build decides that from the tools it
advertises; acceptance predicts them before anything connects (#484), from
the config's `tools`, its `mcp_servers`, and a tool-mode knowledge base —
an inline config's own `knowledge_bases`, or a stored agent's links, which
live outside its config (#260) and are read with it. MCP servers count as
tools **before discovery**, as at the API's save: acceptance runs before
any server connects, so it cannot know what discovery will return. An agent
whose servers answer with no tools (down, or past
`MCP_CONNECT_TIMEOUT_SECONDS`) builds no delegate, yet is refused here for a
broken `llm`. That is the accepted cost: the config is broken either way, and
checking after discovery would put the rule back in the build, which #481
empties.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from turncall.domain.models import AgentConfig, resolve_delegation
from turncall.domain.procedure_graph import procedures_refusal
from turncall.orchestrator import eleven_realtime, grok_voice, xai_config
from turncall.orchestrator.config_warnings import (
    knowledge_mode_warning,
    procedures_on_s2s_warning,
    skills_on_s2s_warning,
)
from turncall.orchestrator.deepgram_flux import MIXED_FLUX_GROUP, mixes_flux


@dataclass(frozen=True)
class AgentWarning:
    """Something an accepted agent's call ignores. `field` when it names one."""

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class AgentRefusal:
    """Why a config cannot run, and the field it names (None: the whole)."""

    reason: str
    field: str | None = None
    # The schema's own errors, whole (`full_schema` only): the agent endpoints
    # answer them as the request validation error they always were.
    schema_errors: tuple[Mapping[str, Any], ...] = ()
    # The 422's `details` when they say more than the field (the `api`
    # origin's project checks); None: `{"field": field}`.
    details: Mapping[str, Any] | None = None

    def describe(self) -> str:
        """The reason with the field it names in front, once."""
        if self.field is None or self.reason.startswith(self.field):
            return self.reason
        return f"{self.field}: {self.reason}"


def unreadable(invalid: ValidationError) -> AgentRefusal:
    """A parse failure as a refusal naming the first error's field."""
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


# A rule reads the config and the knowledge-base modes the call will have:
# the inline config's own, or a stored agent's links (#260), which live
# outside its config.
Rule = Callable[[AgentConfig, Sequence[str]], Iterator[AgentRefusal]]


def first_rule_refusal(
    config: AgentConfig, kb_modes: Sequence[str]
) -> AgentRefusal | None:
    """The first refusal of the first rule that refuses, or None."""
    for rule in _RULES:
        for refusal in rule(config, kb_modes):
            return refusal
    return None


def _builds_an_llm(config: AgentConfig, kb_modes: Sequence[str]) -> bool:
    """Cascade builds the agent's `llm`; so does gpt-live-1 for a client
    delegate (#244). No other S2S agent builds it. Its tools are predicted
    as the module docstring says: `tools`, `mcp_servers` before discovery,
    and a tool-mode knowledge base, inline or linked (#484)."""
    if config.pipeline_mode != "s2s":
        return True
    has_tools = bool(config.tools or config.mcp_servers or "tool" in kb_modes)
    delegation = resolve_delegation(config.s2s, has_tools=has_tools)
    return delegation is not None and delegation.mode == "client"


def _xai_llm(config: AgentConfig, kb_modes: Sequence[str]) -> Iterator[AgentRefusal]:
    if not _builds_an_llm(config, kb_modes):
        return
    members = [("llm", config.llm)] + [
        (f"llm.failover[{i}]", entry) for i, entry in enumerate(config.llm.failover)
    ]
    for field, llm in members:
        if llm.provider == "xai" and (problem := xai_config.llm_refusal(llm)):
            yield AgentRefusal(problem, field)


def _xai_tts(config: AgentConfig, _kb_modes: Sequence[str]) -> Iterator[AgentRefusal]:
    if config.pipeline_mode == "s2s":
        return
    members = [("tts", config.tts)] + [
        (f"tts.failover[{i}]", entry) for i, entry in enumerate(config.tts.failover)
    ]
    for field, tts in members:
        if tts.provider == "xai" and (problem := xai_config.tts_refusal(tts)):
            yield AgentRefusal(problem, field)


def _flux_group(
    config: AgentConfig, _kb_modes: Sequence[str]
) -> Iterator[AgentRefusal]:
    if config.pipeline_mode != "s2s" and mixes_flux(config.stt):
        yield AgentRefusal(MIXED_FLUX_GROUP, "stt.failover")


def _realtime(config: AgentConfig, _kb_modes: Sequence[str]) -> Iterator[AgentRefusal]:
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


def _procedures(
    config: AgentConfig, _kb_modes: Sequence[str]
) -> Iterator[AgentRefusal]:
    """The domain graph rule, as the build applied it: no step-tool check
    (#447 is the API's), and none on S2S, which follows no procedure."""
    if config.pipeline_mode == "s2s":
        return
    if (problem := procedures_refusal(config.procedures)) is not None:
        yield AgentRefusal(problem, "procedures")


_RULES: tuple[Rule, ...] = (_flux_group, _xai_llm, _xai_tts, _realtime, _procedures)


def s2s_warnings(
    config: AgentConfig, kb_modes: Sequence[str]
) -> Iterator[AgentWarning]:
    """What an S2S agent's call ignores. The build logs these itself
    (`skills_skipped`, `procedures_skipped`, the KB drop), so callers don't."""
    for warning in (
        skills_on_s2s_warning(config),
        procedures_on_s2s_warning(config),
        knowledge_mode_warning(config, kb_modes),
    ):
        if warning is not None:
            yield AgentWarning(warning["code"], warning["message"])
