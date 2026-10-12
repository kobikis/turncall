"""`greeting_interruptible: false` keeps a noise at connect from cutting the
greeting off: the caller is muted until the first reply has played.

Seen on builder test calls (2026-10-12): a burst on the mic as the call
connected opened a user turn, cancelled the greeting, and pipecat's empty-turn
rule then had the agent apologise for not catching what nobody said."""

import pytest
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import _user_mute_strategies


@pytest.mark.unit
def test_off_by_default() -> None:
    assert _user_mute_strategies(AgentConfig()) == []


@pytest.mark.unit
def test_false_mutes_the_caller_until_the_first_reply_has_played() -> None:
    (strategy,) = _user_mute_strategies(AgentConfig(greeting_interruptible=False))
    assert isinstance(strategy, MuteUntilFirstBotCompleteUserMuteStrategy)


@pytest.mark.unit
def test_survives_ingest_to_domain() -> None:
    schema = AgentConfigSchema(greeting_interruptible=False)
    assert (
        AgentConfig.model_validate(schema.model_dump()).greeting_interruptible is False
    )
