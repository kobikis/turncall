"""The `openai_live` S2S provider — OpenAI's gpt-live-1 full-duplex model.

Unlike Realtime, the live model listens and speaks at the same time and
handles being interrupted itself, so there is no client-side turn detection
to configure. It can delegate reasoning and tool calls to a backend text
model while the conversation continues.
"""

import pytest
from pydantic import ValidationError

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
        svc = create_s2s_service(_config(), "sk-test")
        assert svc._settings.model == "gpt-live-1"

    def test_explicit_model_is_left_alone(self) -> None:
        svc = create_s2s_service(_config(model="gpt-live-1-mini"), "sk-test")
        assert svc._settings.model == "gpt-live-1-mini"

    def test_voice_is_passed_through(self) -> None:
        svc = create_s2s_service(_config(voice="cedar"), "sk-test")
        assert svc._settings.voice == "cedar"


@pytest.mark.unit
class TestSystemPrompt:
    def test_system_prompt_travels_as_system_instruction(self) -> None:
        svc = create_s2s_service(_config(), "sk-test")
        assert svc._settings.system_instruction == "You are a receptionist."

    def test_first_message_is_folded_in(self) -> None:
        config = AgentConfig(
            system_prompt="You are a receptionist.",
            first_message="Thanks for calling!",
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="openai_live"),
        )
        svc = create_s2s_service(config, "sk-test")
        assert "Thanks for calling!" in svc._settings.system_instruction


@pytest.mark.unit
class TestSampling:
    def test_temperature_is_accepted(self) -> None:
        """Realtime GA rejects temperature; the live model takes it."""
        svc = create_s2s_service(_config(temperature=0.4), "sk-test")
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
        svc = create_s2s_service(_config(), "sk-test")
        assert svc._delegation is None

    def test_backend_model_enables_responses_delegation(self) -> None:
        svc = create_s2s_service(_config(extra={"backend_model": "gpt-6-astra"}), "sk-test")
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
