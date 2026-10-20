"""The `openai_live` S2S provider — OpenAI's gpt-live-1 full-duplex model.

Unlike Realtime, the live model listens and speaks at the same time and
handles being interrupted itself, so there is no client-side turn detection
to configure. It can delegate reasoning and tool calls to a backend text
model while the conversation continues.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import S2SConfigSchema
from turncall.domain.models import AgentConfig, S2SConfig
from turncall.orchestrator.call_session import CallSession, s2s_greeting_frame
from turncall.orchestrator.pipeline_factory import create_pipeline
from turncall.orchestrator.s2s_config import create_s2s_service


def _config(**s2s_kwargs: object) -> AgentConfig:
    return AgentConfig(
        system_prompt="You are a receptionist.",
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="openai_live", **s2s_kwargs),  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestModelAndVoice:
    def test_defaults_to_gpt_live_1(self) -> None:
        """S2SConfig's default model is Realtime's, so it is swapped."""
        svc = create_s2s_service(_config(), creds("sk-test"))
        assert svc._settings.model == "gpt-live-1"

    def test_explicit_model_is_left_alone(self) -> None:
        svc = create_s2s_service(_config(model="gpt-live-1-mini"), creds("sk-test"))
        assert svc._settings.model == "gpt-live-1-mini"

    def test_voice_is_passed_through(self) -> None:
        svc = create_s2s_service(_config(voice="cedar"), creds("sk-test"))
        assert svc._settings.voice == "cedar"


@pytest.mark.unit
class TestSystemPrompt:
    def test_system_prompt_travels_as_system_instruction(self) -> None:
        svc = create_s2s_service(_config(), creds("sk-test"))
        assert svc._settings.system_instruction == "You are a receptionist."

    def test_first_message_is_folded_in(self) -> None:
        config = AgentConfig(
            system_prompt="You are a receptionist.",
            first_message="Thanks for calling!",
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="openai_live"),
        )
        svc = create_s2s_service(config, creds("sk-test"))
        assert "Thanks for calling!" in svc._settings.system_instruction


@pytest.mark.unit
class TestSampling:
    def test_temperature_is_accepted(self) -> None:
        """Realtime GA rejects temperature; the live model takes it."""
        svc = create_s2s_service(_config(temperature=0.4), creds("sk-test"))
        assert svc._settings.temperature == 0.4

    def test_schema_allows_temperature_for_openai_live(self) -> None:
        schema = S2SConfigSchema(provider="openai_live", temperature=0.4)
        assert schema.temperature == 0.4

    def test_schema_still_rejects_temperature_for_realtime(self) -> None:
        with pytest.raises(ValidationError, match="does not support temperature"):
            S2SConfigSchema(provider="openai", temperature=0.4)


@pytest.mark.unit
class TestDelegation:
    def test_no_delegation_by_default(self) -> None:
        svc = create_s2s_service(_config(), creds("sk-test"))
        assert svc._delegation is None

    def test_backend_model_enables_responses_delegation(self) -> None:
        svc = create_s2s_service(
            _config(extra={"backend_model": "gpt-6-astra"}), creds("sk-test")
        )
        assert svc._delegation is not None
        assert svc._delegation.settings.model == "gpt-6-astra"


@pytest.mark.unit
class TestTurnDetection:
    def test_pipecat_vad_is_rejected(self) -> None:
        """Client-side VAD would fight a full-duplex model."""
        with pytest.raises(ValidationError, match="full duplex"):
            S2SConfigSchema(provider="openai_live", turn_detection="pipecat_vad")

    def test_server_vad_is_fine(self) -> None:
        assert S2SConfigSchema(provider="openai_live").turn_detection == "server_vad"


async def _session_context(config: AgentConfig) -> tuple[Any, Any]:
    """The context a call's S2S session starts on, and the built service: the
    greeting frame through the real user aggregator, as `CallSession` sends it."""
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.llm_service import LLMService
    from pipecat.tests.utils import run_test

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", google="g-test"),
    )
    aggregator = next(
        p for p in pipeline.processors if isinstance(p, LLMUserAggregator)
    )
    service = next(
        p for p in pipeline.processors_with_metrics() if isinstance(p, LLMService)
    )
    down, _ = await run_test(aggregator, frames_to_send=[s2s_greeting_frame()])
    (context,) = [f.context for f in down if isinstance(f, LLMContextFrame)]
    return context, service


async def _commentary_at_session_start(config: AgentConfig) -> list[str]:
    """What gpt-live-1 is asked to say once the API reports `session.started`
    — after the session was opened on the call's context."""
    from pipecat.services.openai.live import events

    context, service = await _session_context(config)
    service.send_client_event = AsyncMock()
    await service._handle_context(context)
    (start,) = [c.args[0] for c in service.send_client_event.await_args_list]
    assert isinstance(start, events.SessionStartEvent)
    assert not start.session.input  # the opening is not prior conversation

    service.send_client_event.reset_mock()
    await service._handle_evt_session_started(
        SimpleNamespace(session=SimpleNamespace(id="sess"))
    )
    return [
        c.args[0].content
        for c in service.send_client_event.await_args_list
        if isinstance(c.args[0], events.SessionCommentaryAppendEvent)
    ]


@pytest.mark.unit
@pytest.mark.asyncio
class TestSpeaksFirst:
    """gpt-live-1 is full duplex and does not act on "start the conversation"
    in its instructions until it hears speech, so a caller waiting for the
    greeting heard silence (#248). A trailing developer message in the context
    the session starts on is what pipecat delivers as commentary once the
    session has started — the API's way of asking the model to speak first."""

    async def test_first_message_is_spoken_once_the_session_starts(self) -> None:
        config = AgentConfig(
            system_prompt="You are a receptionist.",
            first_message="Thanks for calling Bella's!",
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="openai_live"),
        )
        (spoken,) = await _commentary_at_session_start(config)
        assert "Thanks for calling Bella's!" in spoken

    async def test_without_a_first_message_it_still_greets(self) -> None:
        assert len(await _commentary_at_session_start(_config())) == 1

    @pytest.mark.parametrize("provider", ["openai", "google"])
    async def test_realtime_and_gemini_start_on_an_empty_context(
        self, provider: str
    ) -> None:
        """They greet from the empty run today, unmeasured with a developer
        message — so they get none."""
        config = AgentConfig(
            first_message="Hi!",
            pipeline_mode="s2s",
            s2s=S2SConfig(provider=provider),  # type: ignore[arg-type]
        )
        context, _ = await _session_context(config)
        assert context.get_messages() == []


def _s2s_call_session(provider: str) -> CallSession:
    return CallSession(
        call_context=MagicMock(),
        transport=MagicMock(),
        pipeline=MagicMock(),
        pipeline_mode="s2s",
        s2s_provider=provider,
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheSessionOpensAtOnce:
    """gpt-live-1 drops the caller's audio until its session starts, and the
    session starts on the greeting frame — so a wait before that frame was
    the caller's first ~3s lost (#256). The service connects in `setup()`,
    before the pipeline processes any frame, so there is nothing to wait for."""

    async def test_the_greeting_frame_is_queued_without_a_wait(self) -> None:
        from pipecat.frames.frames import LLMRunFrame

        worker = MagicMock(queue_frame=AsyncMock())
        await _s2s_call_session("openai_live")._trigger_first_response(worker)

        (frame,) = worker.queue_frame.await_args.args
        assert isinstance(frame, LLMRunFrame)

    @pytest.mark.parametrize("provider", ["openai", "google", "aws"])
    async def test_other_providers_wait_for_the_pipeline_to_start(
        self, provider: str
    ) -> None:
        """They greet on `on_pipeline_started` (#373, test_s2s_greeting.py)."""
        session = _s2s_call_session(provider)
        worker = MagicMock(queue_frame=AsyncMock())
        await session._trigger_first_response(worker)
        await asyncio.sleep(0)

        worker.queue_frame.assert_not_awaited()
        session._greeting_task.cancel()
