"""Resolving what a run actually points at.

Three target forms (#74) and the one rule that makes them worth separating: an
agent row *is* one immutable version, so a target naming an id pins a version
forever, while `agent_name` resolves to whatever is published at run time. An
`inline` target has no row at all (ADR-0017), which is also the sandbox an
operator points a scenario at when its tools have real side effects.

Every target's config goes through agent acceptance (#479): `eval_target` for
a stored agent, by its id so its linked knowledge bases count (#481, as #484
did for a call), `eval_inline` for an inline one, so a config that cannot run
errors the run, naming the field, before any iteration. The target carries the
accepted agent, which is all the build takes.

Split out of `runner.py` for its size. The repositories and acceptance are
imported inside the functions, as they were, so importing this module pulls in
no storage layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from turncall.domain.models import AgentConfig

if TYPE_CHECKING:
    from turncall.services.agent_acceptance import AcceptedAgent


class TargetError(ValueError):
    """The run names a target that cannot be resolved."""


@dataclass(frozen=True)
class ResolvedTarget:
    """What the run actually points at, snapshot included.

    `agent_id` is None for an inline target — ADR-0017's first rule, no locally
    invented sentinel in the column.
    """

    project_id: UUID
    # What agent acceptance answered for the target (#481): the build's input.
    agent: AcceptedAgent
    config_blob: dict[str, Any]
    agent_id: UUID | None = None
    # Which version `agent_id` was. None for an inline target, which has no
    # row and therefore no version (#74).
    agent_version: int | None = None

    @property
    def config(self) -> AgentConfig:
        return self.agent.config


# An agent row is one immutable version, so a target naming an id pins a
# version forever: the moment someone publishes the next one, a scenario
# pinned that way silently stops testing production. `agent_name` is the
# answer — resolved at run time, recorded as the version it found.
_TARGET_TYPES = ("agent", "agent_name", "inline")


async def _resolve_by_id(
    session: Any, *, project_id: UUID, raw_id: Any
) -> ResolvedTarget:
    from turncall.storage.repositories import agent_repo

    if not raw_id:
        raise TargetError("target type 'agent' needs an 'agent_id'")
    agent = await agent_repo.get_agent_by_id(
        session, UUID(str(raw_id)), project_id=project_id
    )
    if agent is None:
        raise TargetError(f"agent {raw_id} not found in this project")
    return await _accepted_row(
        session, agent, project_id=project_id, named=str(agent.id)
    )


async def _resolve_by_name(
    session: Any, *, project_id: UUID, name: Any, environment: str
) -> ResolvedTarget:
    from turncall.storage.repositories import agent_repo

    if not name:
        raise TargetError("target type 'agent_name' needs a 'name'")
    agent = await agent_repo.get_latest_published(
        session, project_id, str(name), environment
    )
    if agent is None:
        # Deliberately not falling back to a draft: "test what is live" is the
        # whole reason to target by name, and quietly testing an unpublished
        # draft instead would answer a different question with the same green
        # tick.
        raise TargetError(f"agent {name!r} has no published version in {environment!r}")
    return await _accepted_row(
        session, agent, project_id=project_id, named=repr(str(name))
    )


async def _accepted(
    source: dict[str, Any] | UUID,
    *,
    project_id: UUID,
    inline: bool,
    refused: str,
    session: Any = None,
) -> AcceptedAgent:
    """`blob` through agent acceptance (#479); a refusal is a `TargetError`
    starting `refused`, naming the field — the run errors before an iteration
    rather than halfway into a conversation."""
    from turncall.services.agent_acceptance import (
        AgentOrigin,
        AgentRefusal,
        accept_agent,
    )

    origin = AgentOrigin.EVAL_INLINE if inline else AgentOrigin.EVAL_TARGET
    accepted = await accept_agent(
        source, project_id=project_id, origin=origin, session=session
    )
    if isinstance(accepted, AgentRefusal):
        raise TargetError(f"{refused}: {accepted.describe()}")
    return accepted


async def _accepted_row(
    session: Any, agent: Any, *, project_id: UUID, named: str
) -> ResolvedTarget:
    """A stored agent's config: the build's rules, not the API's schema — it
    was saved through that, perhaps before one of its rules existed. By id,
    so its linked knowledge bases count (#484): a tool-mode link gives an
    `openai_live` agent the delegate whose `llm` the rules then check."""
    accepted = await _accepted(
        agent.id,
        project_id=project_id,
        inline=False,
        refused=f"agent {named} (v{agent.version}) cannot run",
        session=session,
    )
    return ResolvedTarget(
        project_id=project_id,
        agent=accepted,
        config_blob=dict(agent.config_blob or {}),
        agent_id=agent.id,
        agent_version=agent.version,
    )


async def _resolve_inline(*, project_id: UUID, blob: Any) -> ResolvedTarget:
    """An agent config with no row behind it — ADR-0017's case.

    `agent_id` stays None rather than carrying the zero-UUID sentinel: the
    sentinel exists so a *pipeline* can build, and writing it to a column would
    claim a row that does not exist. The config itself is the record, which is
    what `resolved_config` is for.
    """
    if not isinstance(blob, dict) or not blob:
        raise TargetError("target type 'inline' needs an 'agent' configuration")
    # The API's 422 ran the same schema, so this refuses only what slipped
    # past it — a run queued before a schema change, say. Better a clear
    # errored run than a pipeline that fails halfway into a conversation.
    accepted = await _accepted(
        blob,
        project_id=project_id,
        inline=True,
        refused="inline agent configuration is invalid",
    )
    return ResolvedTarget(
        project_id=project_id,
        agent=accepted,
        config_blob=dict(blob),
        agent_id=None,
        agent_version=None,
    )


async def resolve_target(
    session: Any, *, project_id: UUID, target: dict[str, Any]
) -> ResolvedTarget:
    """Resolve a run's target into the config that will actually run.

    Three forms (#74): a pinned `agent_id`, an `agent_name` resolved to
    whatever is published right now, and an `inline` config with no row at all
    — which is also the sandbox an operator points a scenario at when its tools
    have real side effects.
    """
    kind = target.get("type")
    if kind == "agent":
        return await _resolve_by_id(
            session, project_id=project_id, raw_id=target.get("agent_id")
        )
    if kind == "agent_name":
        return await _resolve_by_name(
            session,
            project_id=project_id,
            name=target.get("name"),
            environment=str(target.get("environment") or "production"),
        )
    if kind == "inline":
        return await _resolve_inline(project_id=project_id, blob=target.get("agent"))
    raise TargetError(
        f"unsupported target type {kind!r} — one of {', '.join(_TARGET_TYPES)}"
    )
