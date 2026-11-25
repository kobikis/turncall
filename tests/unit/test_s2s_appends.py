"""An appended message reaches a realtime session and is answered (#388, #390).

Two appends that matter go in at the pipeline's source: the S2S idle check-in
(`CallSession._speak_idle_prompt`) and an eval's typed caller turn (RTVI's
`send-text`). Up to pipecat 1.16 the user aggregator took them, pushed the
context downstream, and the Grok and OpenAI Realtime services read that as
nothing to answer: Grok said nothing (measured live,
`tests/live/test_live_xai_tools.py`), and TurnCall carried the append around
the user aggregator (`realtime_appends`, deleted with 1.17).

Since 1.17 the user aggregator in realtime mode passes the append on, the
service takes the messages in from the frame, and the assistant aggregator
records them and asks for the response. These tests run that path through the
processors the pipeline factory builds between the two aggregators, with only
the service's socket replaced: they fail if pipecat stops answering an append
on any of these services, or if a TurnCall processor stops passing it on.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, SpeechOutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.tests.utils import run_test

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import create_pipeline

pytestmark = pytest.mark.unit

_CHECK_IN = {"role": "user", "content": "The caller has gone quiet. Check in."}

# The OpenAI Realtime protocol: Grok Voice, OpenAI Realtime, Eleven Realtime.
_REALTIME_PROTOCOL = ["xai", "openai", "elevenlabs"]


def _between_the_aggregators(provider: str) -> list[FrameProcessor]:
    """The factory's S2S pipeline from the user aggregator to the assistant
    aggregator, both included: everything an append passes through."""
    config = AgentConfig.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": provider}}
    )
    processors = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=False, records=False, has_database=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", google="g", xai="xai-key", elevenlabs="xi"),
    ).processors
    first = next(
        i for i, p in enumerate(processors) if isinstance(p, LLMUserAggregator)
    )
    last = next(
        i for i, p in enumerate(processors) if isinstance(p, LLMAssistantAggregator)
    )
    # The transport's output is the test's MagicMock; it passes every frame.
    return [p for p in processors[first : last + 1] if isinstance(p, FrameProcessor)]


def _service(processors: list[FrameProcessor]) -> Any:
    return next(p for p in processors if isinstance(p, LLMService))


async def _not_connected(*_args: Any, **_kwargs: Any) -> None:
    pass


async def _sent_by_a_realtime_session(provider: str, *, run_llm: bool) -> list[str]:
    """What the service sends xAI / OpenAI / ElevenLabs for one append, its
    session already set up — a call under way."""
    processors = _between_the_aggregators(provider)
    service = _service(processors)
    sent: list[str] = []

    async def _record(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    service._connect = service._disconnect = _not_connected
    service._ws_send = _record
    service._api_session_ready = True
    service._llm_needs_conversation_setup = False
    service._context = processors[0].context
    await run_test(
        Pipeline(processors),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=run_llm)],
    )
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_the_service_sends_the_message_and_asks_for_a_reply(
    provider: str,
) -> None:
    sent = await _sent_by_a_realtime_session(provider, run_llm=True)

    assert sent == ["conversation.item.create", "response.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_a_message_not_to_run_is_taken_in_unanswered(provider: str) -> None:
    sent = await _sent_by_a_realtime_session(provider, run_llm=False)

    assert sent == ["conversation.item.create"]


@pytest.mark.asyncio
async def test_gemini_live_takes_it_in_and_completes_the_turn() -> None:
    """Gemini Live used to read the message out of the downstream context; it
    now takes it from the frame and answers on the assistant aggregator's
    upstream context, as the other services do."""
    processors = _between_the_aggregators("google")
    service = _service(processors)
    session = MagicMock(
        send_client_content=AsyncMock(), send_realtime_input=AsyncMock()
    )
    service._connect = service._disconnect = _not_connected
    service._session = session
    service._context = processors[0].context

    await run_test(
        Pipeline(processors),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=True)],
    )

    completes = [
        c.kwargs["turn_complete"] for c in session.send_client_content.await_args_list
    ]
    assert completes == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_it_is_recorded_once_in_the_context(provider: str) -> None:
    """By the assistant aggregator; the user aggregator only passes it on.
    (Eleven Realtime's context opens on its greeting turn, #468.)"""
    processors = _between_the_aggregators(provider)
    service = _service(processors)
    service._connect = service._disconnect = _not_connected
    service._ws_send = AsyncMock()
    service._api_session_ready = True
    service._llm_needs_conversation_setup = False
    context = processors[0].context
    service._context = context

    await run_test(
        Pipeline(processors),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=True)],
    )

    assert context.get_messages().count(_CHECK_IN) == 1
    assert context.get_messages()[-1] == _CHECK_IN


def _optional_s2s_stages() -> list[FrameProcessor]:
    """The S2S stages a default agent does not build: the caller speech timer
    (server_vad), the avatar's speech cutter and the ambience's judge."""
    from turncall.orchestrator.ambience import SpeechBeforeAmbience
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar
    from turncall.orchestrator.caller_speech_timer import CallerSpeechTimer

    return [
        CallerSpeechTimer(MagicMock()),
        SpeechForTheAvatar(),
        SpeechBeforeAmbience(),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", _optional_s2s_stages(), ids=lambda s: type(s).__name__
)
async def test_the_optional_s2s_stages_pass_it_on(stage: FrameProcessor) -> None:
    append = LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=True)
    down, _ = await run_test(stage, frames_to_send=[append])

    carried = [f for f in down if not isinstance(f, SpeechOutputAudioRawFrame)]
    assert carried == [append]
