"""The `openai_live` S2S provider — OpenAI's gpt-live-1 full-duplex model.

Unlike Realtime, the live model listens and speaks at the same time and
handles being interrupted itself, so there is no client-side turn detection
to configure. It can delegate reasoning and tool calls to a backend text
model while the conversation continues.
"""

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import S2SConfigSchema
from turncall.domain.models import AgentConfig, S2SConfig
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


async def _opening(config: AgentConfig) -> tuple[list[dict], list]:
    """Start a session the way a call does, up to where the model is told to
    speak: the greeting frame through the real user aggregator, its context
    into the service, then the API's `session.started`. Returns the context's
    messages and every event the service sent."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.llm_service import LLMService
    from pipecat.tests.utils import run_test

    from turncall.orchestrator.call_session import s2s_greeting_frame
    from turncall.orchestrator.pipeline_factory import create_pipeline

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
    down, _ = await run_test(aggregator, frames_to_send=[s2s_greeting_frame()])
    (context,) = [f.context for f in down if isinstance(f, LLMContextFrame)]
    if config.s2s.provider != "openai_live":
        return context.get_messages(), []

    service = next(
        p for p in pipeline.processors_with_metrics() if isinstance(p, LLMService)
    )
    service.send_client_event = AsyncMock()
    await service._handle_context(context)
    await service._handle_evt_session_started(
        SimpleNamespace(session=SimpleNamespace(id="sess"))
    )
    sent = [c.args[0] for c in service.send_client_event.await_args_list]
    return context.get_messages(), sent


def _commentary(sent: list) -> list[str]:
    return [
        e.content for e in sent if type(e).__name__ == "SessionCommentaryAppendEvent"
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
        _, sent = await _opening(config)
        (start, *_) = sent
        assert type(start).__name__ == "SessionStartEvent"
        assert not start.session.input  # the opening is not prior conversation
        (spoken,) = _commentary(sent)
        assert "Thanks for calling Bella's!" in spoken

    async def test_without_a_first_message_it_still_greets(self) -> None:
        _, sent = await _opening(_config())
        assert len(_commentary(sent)) == 1

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
        messages, _ = await _opening(config)
        assert messages == []
