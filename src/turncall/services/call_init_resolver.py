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

from turncall.storage.repositories import agent_repo


@dataclass(frozen=True)
class CallInitResult:
    """Immutable result of parsing a call-init response."""

    agent: Any | None = None  # AgentRow or None
    dynamic_config: dict[str, Any] | None = None
    template_variables: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    knowledge_context: str | None = None


async def resolve_call_init(
    session: AsyncSession,
    response_data: dict[str, Any],
) -> CallInitResult:
    """Parse a call-init response into a structured result.

    The response may contain:
      - agent_id: UUID of an existing agent to load
      - agent: inline AgentConfig dict (dynamic config)
      - variables: dict of template variables for prompt rendering
      - metadata: arbitrary dict stored on the call record
      - dynamic_data.knowledge_context: string prepended to system prompt

    Args:
        session: Active database session for agent lookup.
        response_data: The JSON response body from the developer's server.

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
            agent = await agent_repo.get_agent_by_id(session, UUID(resolved_id))
        except (ValueError, TypeError):
            logger.warning(
                "call_init_invalid_agent_id",
                agent_id=resolved_id,
            )
    else:
        inline = response_data.get("agent") or response_data.get("assistant")
        if inline:
            dynamic_config = inline

    return CallInitResult(
        agent=agent,
        dynamic_config=dynamic_config,
        template_variables=template_variables,
        metadata=metadata,
        knowledge_context=knowledge_context,
    )
