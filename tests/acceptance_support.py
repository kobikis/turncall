"""What the agent-acceptance unit tests share: the project, the origin
and the outcome helpers."""

import uuid
from typing import Any

from turncall.services.agent_acceptance import (
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
)

PROJECT = uuid.uuid4()
OTHER_PROJECT = uuid.uuid4()
CALL_INIT = AgentOrigin.CALL_INIT


async def accept(source: Any, **kwargs: Any) -> AcceptedAgent | AgentRefusal:
    return await accept_agent(
        source,
        project_id=kwargs.pop("project_id", PROJECT),
        origin=kwargs.pop("origin", CALL_INIT),
        session=kwargs.pop("session", None),
    )


async def refused(config: dict[str, Any]) -> AgentRefusal:
    outcome = await accept(config)
    assert isinstance(outcome, AgentRefusal), outcome
    return outcome


async def accepted(config: dict[str, Any]) -> AcceptedAgent:
    outcome = await accept(config)
    assert isinstance(outcome, AcceptedAgent), outcome
    return outcome


def codes(outcome: AcceptedAgent) -> list[str]:
    return [warning.code for warning in outcome.warnings]
