"""A handoff's target through agent acceptance (#480), and what the model
reads when a handoff cannot be prepared."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from turncall.services.agent_acceptance import (
    AgentOrigin,
    AgentRefusal,
    accept_agent_row,
    log_refusal,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.domain.models import AgentConfig
    from turncall.orchestrator.pipeline_factory import CallContext
    from turncall.storage.models import AgentRow


class HandoffNotPrepared(NamedTuple):
    """Why a handoff was not prepared, as the model and the API read it: the
    target is not one of the project's, its config is refused by agent
    acceptance (#480, in the refusal's own words), or it could not be built.
    Nothing was committed, so the call stays on its current agent."""

    message: str


# A handoff that could not be prepared is never committed: the record would
# say handed off while the pipeline stayed on the current agent. #144.
def not_prepared(why: str) -> str:
    return f"Cannot hand off: {why}; the call stays on the current agent"


def not_prepared_result(not_prepared: HandoffNotPrepared) -> str:
    return json.dumps(
        {"success": False, "message": not_prepared.message, "details": None}
    )


async def accept_target(
    session: AsyncSession, target: AgentRow, call_context: CallContext
) -> AgentConfig | HandoffNotPrepared:
    """Agent acceptance for a handoff's stored target (#480): its config as
    the build would read it, or the refusal's reason, logged. The row the
    handoff already read, with its linked knowledge bases counted (#484)."""
    outcome = await accept_agent_row(
        session,
        target,
        agent_id=target.id,
        project_id=call_context.project_id,
        origin=AgentOrigin.HANDOFF,
    )
    if isinstance(outcome, AgentRefusal):
        log_refusal(
            outcome,
            origin=AgentOrigin.HANDOFF,
            project_id=call_context.project_id,
            call_id=str(call_context.call_id),
            target_agent_id=str(target.id),
        )
        return HandoffNotPrepared(
            not_prepared(f"the target agent's config is refused: {outcome.reason}")
        )
    return outcome.config
