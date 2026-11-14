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
)
from turncall.storage.repositories import agent_repo


@dataclass(frozen=True)
class CallInitResult:
    """Immutable result of parsing a call-init response."""

    agent: Any | None = None  # AgentRow or None
    dynamic_config: dict[str, Any] | None = None
    template_variables: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    knowledge_context: str | None = None
    # The agent id the answer named, as it named it (`agent_id`, or the
    # deprecated `assistant_id`), whether or not it resolved.
    agent_ref: Any | None = None


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

    An agent id resolves only within `project_id`, the project of the number
    (or WebRTC request) call-init ran for (#475): an id is not a capability —
    it rides in webhooks, call reads and logs — so another project's resolves
    to no agent, and the transport refuses the call as it refuses any answer
    naming no usable agent.

    Args:
        session: Active database session for agent lookup.
        response_data: The JSON response body from the developer's server.
        project_id: The project the call (or text session) belongs to.

    Returns:
        Frozen CallInitResult with all parsed fields.
    """
    # Extract template variables
    template_variables: dict[str, str] = {}
    if "variables" in response_data and isinstance(response_data["variables"], dict):
        template_variables = {
            str(k): str(v) for k, v in response_data["variables"].items()
        }

    # Extract metadata
    metadata: dict[str, Any] = {}
    if "metadata" in response_data and isinstance(response_data["metadata"], dict):
        metadata = response_data["metadata"]

    # Extract knowledge_context from dynamic_data
    knowledge_context: str | None = None
    dynamic_data = response_data.get("dynamic_data")
    if isinstance(dynamic_data, dict):
        kc = dynamic_data.get("knowledge_context")
        if isinstance(kc, str) and kc.strip():
            knowledge_context = kc.strip()

    # Resolve agent (by ID or inline config)
    # Accept both "agent_id"/"agent" (new) and "assistant_id"/"assistant" (deprecated)
    agent = None
    dynamic_config = None

    resolved_id = response_data.get("agent_id") or response_data.get("assistant_id")
    if resolved_id:
        try:
            agent = await agent_repo.get_agent_by_id(
                session, UUID(resolved_id), project_id=project_id
            )
        except (ValueError, TypeError):
            logger.warning(
                "call_init_invalid_agent_id",
                agent_id=resolved_id,
            )
        else:
            if agent is None:
                # Never the agent's config: it may be another project's.
                logger.warning(
                    "call_init_agent_not_in_project: no agent runs",
                    agent_id=str(resolved_id),
                    project_id=str(project_id),
                )
    else:
        inline = response_data.get("agent") or response_data.get("assistant")
        if inline:
            dynamic_config = inline

    return CallInitResult(
        agent=agent,
        agent_ref=resolved_id or None,
        dynamic_config=dynamic_config,
        template_variables=template_variables,
        metadata=metadata,
        knowledge_context=knowledge_context,
    )


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
        logger.warning(
            "agent_refused origin={origin} project_id={project_id} "
            "field={field}: {reason}",
            origin=AgentOrigin.CALL_INIT.value,
            project_id=str(project_id),
            field=outcome.field,
            reason=outcome.reason,
            **context,
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
    if result.dynamic_config is not None:
        source: Any = result.dynamic_config
    elif result.agent_ref is not None:
        try:
            source = UUID(str(result.agent_ref))
        except ValueError:
            return AgentRefusal(
                f"agent_id {result.agent_ref!r} is not a UUID", field="agent_id"
            )
    else:
        return None
    return await accept_agent(
        source, project_id=project_id, origin=AgentOrigin.CALL_INIT, session=session
    )
