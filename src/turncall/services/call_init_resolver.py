"""Shared resolver for call-init webhook responses.

Parses the response from a developer's server URL and returns a structured
result containing the resolved agent, dynamic config, template variables,
metadata, and knowledge context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.services.agent_acceptance import (
    UNKNOWN_FIELD,
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
    accept_agent_row,
    log_refusal,
)
from turncall.storage.models import AgentRow
from turncall.storage.repositories import agent_repo


@dataclass(frozen=True)
class CallInitResult:
    """Immutable result of parsing a call-init response."""

    agent: AgentRow | None = None
    dynamic_config: dict[str, Any] | None = None
    template_variables: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    knowledge_context: str | None = None
    # The agent id the answer named, as it named it (`agent_id`, or the
    # deprecated `assistant_id`), whether or not it resolved.
    agent_ref: str | None = None
    # `agent_ref` parsed, whether or not it resolved; None when the answer
    # named none or one that is not a UUID.
    agent_ref_id: UUID | None = None


async def resolve_call_init(
    session: AsyncSession,
    response_data: dict[str, Any],
    *,
    project_id: UUID,
) -> CallInitResult:
    """Parse a call-init response into a structured result.

    The response may contain:
      - agent_id: UUID of an existing agent to load
      - agent: inline AgentConfig dict (dynamic config)
      - variables: dict of template variables for prompt rendering
      - metadata: arbitrary dict stored on the call record
      - dynamic_data.knowledge_context: string prepended to system prompt

    Both "agent_id"/"agent" (new) and "assistant_id"/"assistant" (deprecated)
    are read. An agent id resolves only within `project_id`, the project of
    the number (or WebRTC request) call-init ran for (#475): an id is not a
    capability — it rides in webhooks, call reads and logs — so another
    project's resolves to no agent, and the transport refuses the call as it
    refuses any answer naming no usable agent.
    """
    named = response_data.get("agent_id") or response_data.get("assistant_id")
    agent_ref = str(named) if named else None
    agent, agent_ref_id = None, None
    dynamic_config = None
    if agent_ref is not None:
        agent, agent_ref_id = await _named_agent(session, agent_ref, project_id)
    else:
        dynamic_config = response_data.get("agent") or response_data.get("assistant")
    return CallInitResult(
        agent=agent,
        agent_ref=agent_ref,
        agent_ref_id=agent_ref_id,
        dynamic_config=dynamic_config or None,
        template_variables=_template_variables(response_data),
        metadata=_metadata(response_data),
        knowledge_context=_knowledge_context(response_data),
    )


def _template_variables(response_data: dict[str, Any]) -> dict[str, str]:
    variables = response_data.get("variables")
    if not isinstance(variables, dict):
        return {}
    return {str(k): str(v) for k, v in variables.items()}


def _metadata(response_data: dict[str, Any]) -> dict[str, Any]:
    metadata = response_data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _knowledge_context(response_data: dict[str, Any]) -> str | None:
    dynamic_data = response_data.get("dynamic_data")
    if not isinstance(dynamic_data, dict):
        return None
    kc = dynamic_data.get("knowledge_context")
    return kc.strip() if isinstance(kc, str) and kc.strip() else None


async def _named_agent(
    session: AsyncSession, agent_ref: str, project_id: UUID
) -> tuple[AgentRow | None, UUID | None]:
    """The project's agent `agent_ref` names, and the id parsed (None when it
    is not a UUID). Read once: acceptance takes this row."""
    try:
        agent_id = UUID(agent_ref)
    except ValueError:
        logger.warning("call_init_invalid_agent_id", agent_id=agent_ref)
        return None, None
    agent = await agent_repo.get_agent_by_id(session, agent_id, project_id=project_id)
    if agent is None:
        # Never the agent's config: it may be another project's.
        logger.warning(
            "call_init_agent_not_in_project: no agent runs",
            agent_id=agent_ref,
            project_id=str(project_id),
        )
    return agent, agent_id


async def accept_call_init_agent(
    session: AsyncSession,
    result: CallInitResult,
    *,
    project_id: UUID,
    **context: str,
) -> AcceptedAgent | AgentRefusal | None:
    """Agent acceptance (#476) for the agent a call-init answer named, or
    None when it named none.

    An inline config is accepted as `call_init`; an agent id must be one of
    `project_id`'s. A refusal is logged with the origin, the project and the
    reason, plus `context` (the call or session it was for). Of the warnings
    only an unknown field is logged here: the build logs the S2S ones itself
    when it skips what they name.
    """
    outcome = await _accept_named(session, result, project_id=project_id)
    if isinstance(outcome, AgentRefusal):
        log_refusal(
            outcome, origin=AgentOrigin.CALL_INIT, project_id=project_id, **context
        )
    elif isinstance(outcome, AcceptedAgent):
        for warning in outcome.warnings:
            if warning.code == UNKNOWN_FIELD:
                logger.warning(
                    "agent_unknown_field origin={origin} project_id={project_id}"
                    ": {message}",
                    origin=AgentOrigin.CALL_INIT.value,
                    project_id=str(project_id),
                    field=warning.field,
                    message=warning.message,
                    **context,
                )
    return outcome


async def _accept_named(
    session: AsyncSession, result: CallInitResult, *, project_id: UUID
) -> AcceptedAgent | AgentRefusal | None:
    """The answer's inline agent, or the row `resolve_call_init` read for its
    agent id (not read again), through acceptance as `call_init`."""
    if result.dynamic_config is not None:
        return await accept_agent(
            result.dynamic_config,
            project_id=project_id,
            origin=AgentOrigin.CALL_INIT,
            session=session,
        )
    if result.agent_ref is None:
        return None
    if result.agent_ref_id is None:
        return AgentRefusal(
            f"agent_id {result.agent_ref!r} is not a UUID", field="agent_id"
        )
    return await accept_agent_row(
        session,
        result.agent,
        agent_id=result.agent_ref_id,
        project_id=project_id,
        origin=AgentOrigin.CALL_INIT,
    )
