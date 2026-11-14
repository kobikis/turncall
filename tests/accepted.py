"""An accepted agent for a test that builds a pipeline (#481).

The build takes only what agent acceptance answered, so a test hands it a
config the same way: through acceptance, for the test's origin. A config the
origin refuses fails the test here, naming why, rather than reaching a build
that no longer checks it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from turncall.domain.models import AgentConfig
from turncall.services.agent_acceptance import (
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_raw_config,
)


def accepted(
    config: AgentConfig | Mapping[str, Any] | None = None,
    *,
    origin: AgentOrigin = AgentOrigin.CALL_INIT,
) -> AcceptedAgent:
    """`config` (a domain config, a raw mapping, or None for the defaults)
    accepted as `origin`, which takes a raw config: `call_init` by default."""
    if config is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(config, AgentConfig):
        raw = config.model_dump()
    else:
        raw = config
    outcome = accept_raw_config(raw, origin=origin)
    if isinstance(outcome, AgentRefusal):
        raise AssertionError(f"the test's agent is refused: {outcome.describe()}")
    return outcome


def refusal(
    config: AgentConfig | Mapping[str, Any],
    *,
    origin: AgentOrigin = AgentOrigin.CALL_INIT,
) -> AgentRefusal:
    """Why acceptance refuses `config` as `origin` — what a call-init inline
    agent breaking a rule gets, where the build once failed it (#481)."""
    raw = config.model_dump() if isinstance(config, AgentConfig) else config
    outcome = accept_raw_config(raw, origin=origin)
    if not isinstance(outcome, AgentRefusal):
        raise AssertionError("the test's agent was accepted")
    return outcome
