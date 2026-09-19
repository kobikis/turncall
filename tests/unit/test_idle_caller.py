"""The idle guard: two strikes, then the call ends as `customer_silent`.

Pipecat's `user_idle_timeout` only reports that the caller has gone quiet after
the agent stopped speaking. Everything about what that means — nudge once, give
up on the second, reset when they come back, and record why the call ended —
is TurnCall's.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from turncall.domain.call_state import infer_ended_reason
from turncall.domain.enums import CallStatus, EndedReason

_SILENT = "call.customer_silent"
_MAX_DURATION = "call.max_duration_reached"


def _session(*, pipeline_mode: str = "cascade"):
    """A CallSession with the pipeline and worker stubbed out."""
    from turncall.orchestrator.call_session import CallSession

    handlers: dict[str, object] = {}

    aggregator = MagicMock()
    aggregator.event_handler = lambda name: lambda fn: handlers.setdefault(name, fn)

    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )

    aggregator.__class__ = LLMUserAggregator

    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = [MagicMock(), aggregator]

    context = MagicMock()
    context.call_id = uuid4()

    session = CallSession(
        call_context=context,
        transport=MagicMock(),
        pipeline=pipeline,
        pipeline_mode=pipeline_mode,
        idle_message="Are you still there?",
    )
    session._task = MagicMock()
    session._task.queue_frame = AsyncMock()
    session._task.cancel = AsyncMock()
    session._log_event = AsyncMock()
    session._arm_idle_guard()
    return session, handlers


@pytest.mark.unit
class TestTheStrikeCount:
    async def test_the_first_silence_speaks_rather_than_hangs_up(self) -> None:
        session, handlers = _session()

        await handlers["on_user_turn_idle"]()

        session._task.queue_frame.assert_awaited_once()
        session._task.cancel.assert_not_awaited()
        session._log_event.assert_not_awaited()

    async def test_the_second_silence_ends_the_call(self) -> None:
        session, handlers = _session()

        await handlers["on_user_turn_idle"]()
        await handlers["on_user_turn_idle"]()

        session._task.cancel.assert_awaited_once()
        event_type, payload = session._log_event.await_args.args
        assert event_type.value == _SILENT
        assert payload == {"strikes": 2}

    async def test_speaking_again_resets_the_count(self) -> None:
        """A caller who pauses, answers, then pauses again must not be hung up
        on that second pause: strikes count consecutive silences."""
        session, handlers = _session()

        await handlers["on_user_turn_idle"]()
        await handlers["on_user_turn_started"]()
        await handlers["on_user_turn_idle"]()

        session._task.cancel.assert_not_awaited()
        assert session._task.queue_frame.await_count == 2


@pytest.mark.unit
class TestTheNudgeFitsTheTransport:
    async def test_cascade_speaks_the_configured_line(self) -> None:
        session, handlers = _session(pipeline_mode="cascade")

        await handlers["on_user_turn_idle"]()

        frame = session._task.queue_frame.await_args.args[0]
        assert type(frame).__name__ == "TTSSpeakFrame"
        assert frame.text == "Are you still there?"

    async def test_s2s_asks_the_model_because_it_has_no_tts_stage(self) -> None:
        """No realtime service handles a TTSSpeakFrame, so a literal line
        cannot be spoken — the model is asked to check in instead."""
        session, handlers = _session(pipeline_mode="s2s")

        await handlers["on_user_turn_idle"]()

        frame = session._task.queue_frame.await_args.args[0]
        assert type(frame).__name__ == "LLMMessagesAppendFrame"
        assert "gone quiet" in frame.messages[0]["content"]


@pytest.mark.unit
class TestTheEndedReason:
    def test_silence_beats_assistant_ended(self) -> None:
        """Ending the call is *how* the guard gives up, so the call is
        assistant-ended by mechanism. Reported that way it would say the agent
        chose to leave, rather than that the caller went quiet."""
        assert (
            infer_ended_reason(CallStatus.COMPLETED, {_SILENT}, assistant_ended=True)
            == EndedReason.CUSTOMER_SILENT
        )

    def test_the_duration_cap_still_wins(self) -> None:
        """A call that hit the cap left because of the cap, even if the caller
        had also gone quiet."""
        assert (
            infer_ended_reason(
                CallStatus.COMPLETED, {_SILENT, _MAX_DURATION}, assistant_ended=True
            )
            == EndedReason.MAX_DURATION_REACHED
        )

    def test_voicemail_and_transfer_still_win(self) -> None:
        for event in ("voicemail.detected", "call.transferred"):
            reason = infer_ended_reason(
                CallStatus.COMPLETED, {_SILENT, event}, assistant_ended=False
            )
            assert reason != EndedReason.CUSTOMER_SILENT

    def test_without_the_event_nothing_changes(self) -> None:
        assert (
            infer_ended_reason(CallStatus.COMPLETED, set(), assistant_ended=False)
            == EndedReason.CUSTOMER_ENDED_CALL
        )


@pytest.mark.unit
class TestTheTimerIsActuallyWired:
    """The tests above mock the aggregator, so they prove the reaction and not
    that anything finds a real one or arms a real timer."""

    @staticmethod
    def _user_aggregator(mocker, **overrides):
        from loguru import logger
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMUserAggregator,
        )

        from turncall.domain.models import AgentConfig
        from turncall.orchestrator import pipeline_factory

        captured: list = []

        def _capture(processors):
            captured.extend(processors)
            return mocker.MagicMock()

        mocker.patch.object(pipeline_factory, "Pipeline", _capture)
        try:
            pipeline_factory.create_pipeline(
                config=AgentConfig(**overrides),
                transport=mocker.MagicMock(),
                call_context=mocker.MagicMock(),
                openai_api_key="test-key",
                pipecat_settings=mocker.MagicMock(),
            )
        except Exception as exc:
            logger.debug("pipeline build stopped after assembly: {}", exc)

        return next(
            (p for p in captured if isinstance(p, LLMUserAggregator)),
            None,
        )

    def test_the_cascade_pipeline_has_the_aggregator_the_guard_looks_for(
        self, mocker
    ) -> None:
        aggregator = self._user_aggregator(mocker, pipeline_mode="cascade")

        assert aggregator is not None, (
            "no LLMUserAggregator in the pipeline — _arm_idle_guard would log "
            "a warning and silently never fire"
        )
        assert aggregator._params.user_idle_timeout == 10.0

    def test_s2s_arms_the_timer_on_the_default_turn_detection(self, mocker) -> None:
        """S2S built its aggregator params only for `pipecat_vad`, while
        `server_vad` is the default — so the guard would have been off for most
        S2S agents."""
        aggregator = self._user_aggregator(mocker, pipeline_mode="s2s")

        assert aggregator is not None
        assert aggregator._params.user_idle_timeout == 10.0

    def test_zero_disables_it(self, mocker) -> None:
        aggregator = self._user_aggregator(
            mocker, pipeline_mode="cascade", user_idle_timeout_ms=0
        )

        assert aggregator is not None
        assert aggregator._params.user_idle_timeout == 0
