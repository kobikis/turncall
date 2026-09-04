"""Tests for the OpenRouter LLM provider (fallback routing + voice-only scope)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import LLMConfigSchema
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import _create_llm_service
from turncall.services.llm_text import complete_text


@pytest.mark.unit
class TestFallbackModelsValidation:
    def test_rejected_on_non_openrouter_provider(self) -> None:
        with pytest.raises(ValidationError, match="fallback_models requires provider 'openrouter'"):
            LLMConfigSchema(provider="openai", fallback_models=["openai/gpt-4o"])

    def test_accepted_on_openrouter(self) -> None:
        cfg = LLMConfigSchema(
            provider="openrouter",
            model="anthropic/claude-3.5-sonnet",
            fallback_models=["openai/gpt-4o"],
        )
        assert cfg.fallback_models == ["openai/gpt-4o"]

    def test_openrouter_without_fallbacks_is_valid(self) -> None:
        cfg = LLMConfigSchema(provider="openrouter", model="anthropic/claude-3.5-sonnet")
        assert cfg.fallback_models == []

    def test_empty_fallbacks_on_other_provider_ok(self) -> None:
        # Default empty list must not trip the validator for non-openrouter providers.
        assert LLMConfigSchema(provider="openai").fallback_models == []


def _agent_config(**llm_kwargs: object) -> AgentConfig:
    return AgentConfig(
        system_prompt="hi",
        llm=LLMConfig(**llm_kwargs),  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestOpenRouterFactory:
    def test_builds_models_array_primary_first(self) -> None:
        config = _agent_config(
            provider="openrouter",
            model="anthropic/claude-3.5-sonnet",
            fallback_models=["openai/gpt-4o", "google/gemini-flash-1.5"],
        )
        svc = _create_llm_service(config, "", openrouter_api_key="sk-or-test")
        assert svc._settings.extra == {
            "extra_body": {
                "models": [
                    "anthropic/claude-3.5-sonnet",
                    "openai/gpt-4o",
                    "google/gemini-flash-1.5",
                ]
            }
        }

    def test_no_fallbacks_sends_no_extra_body(self) -> None:
        config = _agent_config(provider="openrouter", model="anthropic/claude-3.5-sonnet")
        svc = _create_llm_service(config, "", openrouter_api_key="sk-or-test")
        assert svc._settings.extra == {}

    def test_per_agent_key_overrides_env(self) -> None:
        config = _agent_config(
            provider="openrouter", model="x", api_key="sk-or-agent"
        )
        svc = _create_llm_service(config, "", openrouter_api_key="sk-or-env")
        assert svc._client.api_key == "sk-or-agent"


@pytest.mark.unit
class TestOpenRouterTextPath:
    """complete_text must support openrouter for internal callers (post-call analysis).

    Regression: a blanket raise here broke analysis of openrouter voice agents.
    """

    @pytest.mark.asyncio
    async def test_complete_text_routes_to_openrouter_with_models_array(self) -> None:
        config = LLMConfig(
            provider="openrouter",
            model="anthropic/claude-3.5-sonnet",
            fallback_models=["openai/gpt-4o"],
            api_key="sk-or-agent",
        )
        mock_response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 7},
            },
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        with patch("turncall.services.llm_text.get_http_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get.return_value = mock_client

            result = await complete_text(config, [{"role": "user", "content": "hi"}])

        assert result.text == "ok"
        url = mock_client.post.call_args.args[0]
        body = mock_client.post.call_args.kwargs["json"]
        headers = mock_client.post.call_args.kwargs["headers"]
        assert url == "https://openrouter.ai/api/v1/chat/completions"
        assert body["models"] == ["anthropic/claude-3.5-sonnet", "openai/gpt-4o"]
        assert headers["Authorization"] == "Bearer sk-or-agent"
