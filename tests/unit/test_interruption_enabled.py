"""`interruption_enabled: false` must actually stop barge-in.

The field was accepted by the API, survived a publish, appeared in the OpenAPI
spec — and was read by nothing, so the caller could always talk over the agent.
Pipecat spells the control `enable_interruptions` on the user's turn-start
strategy, which is what decides whether speech mid-response broadcasts an
interruption.
"""

from unittest.mock import MagicMock

import pytest

from turncall.domain.models import AgentConfig, S2SConfig


def _strategies(config: AgentConfig, *, smart_turn: bool = True):
    """The extracted builder — no transport, no API keys, no pipeline."""
    from turncall.orchestrator.pipeline_factory import _build_turn_strategies

    return _build_turn_strategies(config, smart_turn=smart_turn)


def _start_of(strategies):
    return getattr(strategies, "start", None) if strategies else None


@pytest.mark.unit
class TestCascade:
    def test_disabling_it_reaches_the_turn_start_strategy(self) -> None:
        start = _start_of(_strategies(AgentConfig(interruption_enabled=False)))

        assert start, "no turn-start strategy was configured"
        # Both of Pipecat's defaults, not just the VAD one: supplying a list
        # replaces the pair, so passing one would drop transcription-driven
        # turn start along with barge-in.
        assert [type(x).__name__ for x in start] == [
            "VADUserTurnStartStrategy",
            "TranscriptionUserTurnStartStrategy",
        ]
        assert all(x._enable_interruptions is False for x in start)

    def test_the_default_leaves_barge_in_on(self) -> None:
        """An agent that never set the field must not acquire a start strategy
        at all, so Pipecat's own defaults (which interrupt) stand. Checked with
        smart turn off, since that is the other thing that builds this object."""
        assert (
            _strategies(AgentConfig(smart_turn_detection=False), smart_turn=True)
            is None
        )

    def test_it_composes_with_smart_turn_rather_than_replacing_it(self) -> None:
        """Both land on one UserTurnStrategies. Assigning it twice would drop
        whichever was written first — silently losing turn detection."""
        strategies = _strategies(
            AgentConfig(interruption_enabled=False, smart_turn_detection=True)
        )

        assert all(x._enable_interruptions is False for x in strategies.start)
        assert strategies.stop, "smart turn's stop strategy was dropped"

    def test_smart_turn_alone_does_not_disable_barge_in(self) -> None:
        """UserTurnStrategies fills `start` with its own defaults whenever it is
        built, so the observable is that those defaults still interrupt — not
        that the list is empty."""
        strategies = _strategies(AgentConfig(smart_turn_detection=True))

        assert strategies.stop
        assert all(x._enable_interruptions for x in strategies.start)


@pytest.mark.unit
class TestS2S:
    def test_it_is_not_applied_and_says_so(self) -> None:
        """Not wired on S2S on purpose: supplying user_turn_strategies would
        discard what realtime_service_mode auto-swaps in, and on server-side
        turn detection the provider owns turn-taking anyway. A warning beats a
        setting that looks applied — which is the bug this file is about."""
        from loguru import logger

        from turncall.orchestrator.pipeline_factory import _create_s2s_pipeline

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(m), level="WARNING")
        try:
            _create_s2s_pipeline(
                config=AgentConfig(
                    pipeline_mode="s2s",
                    interruption_enabled=False,
                    s2s=S2SConfig(provider="openai", turn_detection="server_vad"),
                ),
                transport=MagicMock(),
                call_context=MagicMock(),
                openai_api_key="",
                pipecat_settings=MagicMock(),
            )
        finally:
            logger.remove(sink)

        assert any("not applied on S2S" in m for m in messages), messages
