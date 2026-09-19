"""`LLMConfig.model` used to hold OpenAI's model for every provider.

The default was `gpt-4o-mini` in a provider-agnostic field, passed straight
through by all six provider branches. An agent that named no model sent it to
whichever provider it had chosen:

    anthropic  404 not_found_error: model: gpt-4o-mini

It corrupted a second decision too. `is_anthropic_model(config.llm.model)`
decides whether Bedrock is sent a `temperature` (ADR-0014); a Bedrock agent
carrying the leaked OpenAI name reads as non-Anthropic, so a Claude model
would have been sent the parameter the rule exists to withhold.
"""

import pytest

from turncall.services.llm_models import model_is_required, resolve_llm_model


@pytest.mark.unit
class TestProvidersWithAHouseModel:
    def test_openai_keeps_its_own(self) -> None:
        assert resolve_llm_model("openai", "") == "gpt-4o-mini"

    def test_anthropic_gets_a_claude_model(self) -> None:
        assert "claude" in resolve_llm_model("anthropic", "")

    def test_an_explicit_model_is_passed_through(self) -> None:
        """A wrong model the agent actually chose is reported by the provider,
        not silently replaced — adr/0016's rule."""
        assert resolve_llm_model("anthropic", "claude-opus-5") == "claude-opus-5"
        assert resolve_llm_model("openai", "gpt-4.1") == "gpt-4.1"


@pytest.mark.unit
class TestProvidersWhereTheModelIsTheDeployment:
    @pytest.mark.parametrize(
        "provider", ["ollama", "custom_openai", "bedrock", "openrouter"]
    )
    def test_an_unnamed_model_raises_rather_than_guessing(self, provider: str) -> None:
        """Guessing here trades a clear error for a confusing one: an Ollama
        model is whatever is pulled onto that host, a Bedrock id's
        availability is region-specific, a BYOM endpoint decides for itself,
        and OpenRouter exists to route between vendors."""
        with pytest.raises(ValueError, match=f"required for provider '{provider}'"):
            resolve_llm_model(provider, "")

    @pytest.mark.parametrize(
        "provider", ["ollama", "custom_openai", "bedrock", "openrouter"]
    )
    def test_the_error_says_why(self, provider: str) -> None:
        with pytest.raises(ValueError) as caught:
            resolve_llm_model(provider, "")

        assert len(str(caught.value)) > 60, "the message should explain, not just deny"

    def test_a_named_model_is_fine(self) -> None:
        assert resolve_llm_model("bedrock", "anthropic.claude-sonnet-5") == (
            "anthropic.claude-sonnet-5"
        )
        assert resolve_llm_model("ollama", "llama3") == "llama3"

    def test_model_is_required_matches(self) -> None:
        assert model_is_required("bedrock")
        assert not model_is_required("openai")


@pytest.mark.unit
class TestTheLegacySentinel:
    """`model_dump()` persisted `gpt-4o-mini` into the config_blob of every
    agent created before the default was cleared, so it has to be read as
    "unset" everywhere but OpenAI — or those agents stay broken."""

    def test_anthropic_heals(self) -> None:
        assert "claude" in resolve_llm_model("anthropic", "gpt-4o-mini")

    def test_openai_keeps_it_because_there_it_is_correct(self) -> None:
        assert resolve_llm_model("openai", "gpt-4o-mini") == "gpt-4o-mini"

    def test_a_required_provider_raises_instead_of_carrying_it(self) -> None:
        """Sending `gpt-4o-mini` to Ollama asks for a local model by that
        name, which is not what anyone meant."""
        with pytest.raises(ValueError, match="required for provider 'ollama'"):
            resolve_llm_model("ollama", "gpt-4o-mini")


@pytest.mark.unit
class TestTheBoundaryRejectsIt:
    def test_creating_a_bedrock_agent_without_a_model_is_a_422(self) -> None:
        """Caught at create time rather than at the first call, where it would
        be a dead conversation instead of a validation error."""
        from pydantic import ValidationError

        from turncall.api.v1.schemas.agents import LLMConfigSchema

        with pytest.raises(ValidationError, match="model is required"):
            LLMConfigSchema(provider="bedrock")

    def test_an_openai_agent_needs_nothing(self) -> None:
        from turncall.api.v1.schemas.agents import LLMConfigSchema

        assert LLMConfigSchema(provider="openai").model == ""


@pytest.mark.unit
class TestTheResolvedModelReachesTheService:
    def test_the_anthropic_service_is_built_with_a_claude_model(self) -> None:
        from turncall.domain.models import AgentConfig, LLMConfig
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(llm=LLMConfig(provider="anthropic"))
        service = _create_llm_service(config, "", anthropic_api_key="test-key")

        assert "claude" in str(service._settings.model)
