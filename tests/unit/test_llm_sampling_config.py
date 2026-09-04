"""Temperature / max_tokens wiring: cascade LLM, voicemail pin, and S2S fields."""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import S2SConfigSchema
from turncall.domain.models import AgentConfig, LLMConfig, S2SConfig
from turncall.orchestrator.pipeline_factory import _create_llm_service
from turncall.orchestrator.s2s_config import create_s2s_service


def _agent_config(**llm_kwargs: object) -> AgentConfig:
    return AgentConfig(system_prompt="hi", llm=LLMConfig(**llm_kwargs))  # type: ignore[arg-type]


@pytest.mark.unit
class TestCascadeSamplingSettings:
    def test_openai_receives_temperature_and_max_tokens(self) -> None:
        config = _agent_config(provider="openai", temperature=1.3, max_tokens=256)
        svc = _create_llm_service(config, "sk-test")
        assert svc._settings.temperature == 1.3
        assert svc._settings.max_tokens == 256

    def test_defaults_applied_as_stored(self) -> None:
        svc = _create_llm_service(_agent_config(provider="openai"), "sk-test")
        assert svc._settings.temperature == 0.7
        assert svc._settings.max_tokens == 1024

    def test_anthropic_receives_settings(self) -> None:
        config = _agent_config(
            provider="anthropic", model="claude-3-5-haiku-20241022", temperature=0.2, max_tokens=333
        )
        svc = _create_llm_service(config, "", anthropic_api_key="sk-ant-test")
        assert svc._settings.temperature == 0.2
        assert svc._settings.max_tokens == 333

    def test_ollama_receives_settings(self) -> None:
        config = _agent_config(
            provider="ollama", model="gemma3:12b", temperature=0.1, max_tokens=64
        )
        svc = _create_llm_service(config, "")
        assert svc._settings.temperature == 0.1
        assert svc._settings.max_tokens == 64

    def test_custom_openai_receives_settings(self) -> None:
        config = _agent_config(
            provider="custom_openai",
            base_url="http://localhost:9999/v1",
            temperature=1.9,
            max_tokens=42,
        )
        svc = _create_llm_service(config, "")
        assert svc._settings.temperature == 1.9
        assert svc._settings.max_tokens == 42

    def test_openrouter_receives_settings(self) -> None:
        config = _agent_config(
            provider="openrouter", model="openai/gpt-4o", temperature=0.5, max_tokens=99
        )
        svc = _create_llm_service(config, "", openrouter_api_key="sk-or-test")
        assert svc._settings.temperature == 0.5
        assert svc._settings.max_tokens == 99


@pytest.mark.unit
class TestClassifierOverride:
    def test_temperature_override_wins(self) -> None:
        # Voicemail classification pins a deterministic temperature regardless
        # of the agent's conversational setting.
        config = _agent_config(provider="openai", temperature=1.8)
        svc = _create_llm_service(config, "sk-test", temperature=0.1)
        assert svc._settings.temperature == 0.1

    def test_max_tokens_override_wins(self) -> None:
        config = _agent_config(provider="openai", max_tokens=4096)
        svc = _create_llm_service(config, "sk-test", max_tokens=16)
        assert svc._settings.max_tokens == 16


@pytest.mark.unit
class TestReasoningEffort:
    def test_openai_folds_effort_into_extra_body(self) -> None:
        config = _agent_config(provider="openai", model="o4-mini", reasoning_effort="high")
        svc = _create_llm_service(config, "sk-test")
        assert svc._settings.extra == {"extra_body": {"reasoning_effort": "high"}}

    def test_openai_omits_when_unset(self) -> None:
        svc = _create_llm_service(_agent_config(provider="openai"), "sk-test")
        assert svc._settings.extra == {}

    def test_custom_openai_folds_effort(self) -> None:
        config = _agent_config(
            provider="custom_openai",
            base_url="http://localhost:9999/v1",
            reasoning_effort="low",
        )
        svc = _create_llm_service(config, "")
        assert svc._settings.extra == {"extra_body": {"reasoning_effort": "low"}}

    def test_openrouter_keeps_models_alongside_effort(self) -> None:
        config = _agent_config(
            provider="openrouter",
            model="openai/o4-mini",
            fallback_models=["openai/gpt-4o"],
            reasoning_effort="medium",
        )
        svc = _create_llm_service(config, "", openrouter_api_key="sk-or-test")
        assert svc._settings.extra == {
            "extra_body": {
                "models": ["openai/o4-mini", "openai/gpt-4o"],
                "reasoning_effort": "medium",
            }
        }

    def test_classifier_forces_effort_off(self) -> None:
        # The voicemail classifier passes reasoning_effort=None to suppress the
        # agent's setting, even when the agent has one.
        config = _agent_config(provider="openai", reasoning_effort="high")
        svc = _create_llm_service(config, "sk-test", temperature=0.1, reasoning_effort=None)
        assert svc._settings.extra == {}


@pytest.mark.unit
class TestS2SSchemaFields:
    def test_defaults_are_none(self) -> None:
        schema = S2SConfigSchema()
        assert schema.temperature is None
        assert schema.max_tokens is None

    def test_google_accepts_both(self) -> None:
        schema = S2SConfigSchema(
            provider="google",
            model="models/gemini-3.1-flash-live-preview",
            temperature=1.2,
            max_tokens=2048,
        )
        assert schema.temperature == 1.2
        assert schema.max_tokens == 2048

    def test_openai_rejects_temperature(self) -> None:
        with pytest.raises(ValidationError, match="does not support temperature"):
            S2SConfigSchema(provider="openai", temperature=0.8)

    def test_openai_accepts_max_tokens(self) -> None:
        assert S2SConfigSchema(provider="openai", max_tokens=500).max_tokens == 500

    def test_temperature_range(self) -> None:
        with pytest.raises(ValidationError):
            S2SConfigSchema(provider="google", temperature=2.5)

    def test_max_tokens_minimum(self) -> None:
        with pytest.raises(ValidationError):
            S2SConfigSchema(provider="google", max_tokens=0)


def _s2s_agent(**s2s_kwargs: object) -> AgentConfig:
    return AgentConfig(
        system_prompt="hi",
        pipeline_mode="s2s",
        s2s=S2SConfig(**s2s_kwargs),  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestS2SServiceWiring:
    def test_openai_max_tokens_maps_to_max_output_tokens(self) -> None:
        config = _s2s_agent(provider="openai", max_tokens=750)
        svc = create_s2s_service(config, "sk-test")
        props = svc._settings.session_properties
        assert props.max_output_tokens == 750

    def test_openai_unset_leaves_provider_default(self) -> None:
        config = _s2s_agent(provider="openai")
        svc = create_s2s_service(config, "sk-test")
        assert svc._settings.session_properties.max_output_tokens is None

    def test_gemini_receives_both(self) -> None:
        config = _s2s_agent(
            provider="google",
            model="models/gemini-3.1-flash-live-preview",
            temperature=0.4,
            max_tokens=1234,
        )
        svc = create_s2s_service(config, "", google_api_key="g-test")
        assert svc._settings.temperature == 0.4
        assert svc._settings.max_tokens == 1234
