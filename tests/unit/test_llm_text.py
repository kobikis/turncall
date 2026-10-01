"""Tests for LLM text completion service."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from turncall.domain.models import LLMConfig
from turncall.services.credentials import Credentials
from turncall.services.llm_text import (
    CompletionResult,
    _resolve_api_key,
    _resolve_base_url,
    complete_text,
)


@pytest.mark.unit
class TestResolveBaseUrl:
    def test_openai_default(self) -> None:
        config = LLMConfig(provider="openai")
        assert _resolve_base_url(config) == "https://api.openai.com/v1"

    def test_ollama_default(self) -> None:
        config = LLMConfig(provider="ollama")
        assert _resolve_base_url(config) == "http://localhost:11434/v1"

    def test_custom_base_url(self) -> None:
        config = LLMConfig(provider="custom_openai", base_url="https://my-llm.com/v1/")
        assert _resolve_base_url(config) == "https://my-llm.com/v1"

    def test_custom_base_url_strips_trailing_slash(self) -> None:
        config = LLMConfig(base_url="https://example.com/api/")
        assert _resolve_base_url(config) == "https://example.com/api"


@pytest.mark.unit
class TestResolveApiKey:
    """Agent inline, then the project's credential, then the platform's — the
    order a voice call on the same project resolves in (ADR-0020)."""

    def test_config_api_key_takes_precedence(self) -> None:
        config = LLMConfig(api_key="my-custom-key")
        creds = Credentials(
            platform={"openai": "env"}, project={"openai": {"api_key": "p"}}
        )
        assert _resolve_api_key(config, creds) == "my-custom-key"

    def test_project_credential_before_platform(self) -> None:
        config = LLMConfig(provider="anthropic", model="m")
        creds = Credentials(
            platform={"anthropic": "env"}, project={"anthropic": {"api_key": "proj"}}
        )
        assert _resolve_api_key(config, creds) == "proj"

    def test_falls_back_to_platform(self) -> None:
        config = LLMConfig(provider="openai", api_key=None)
        assert (
            _resolve_api_key(config, Credentials(platform={"openai": "env-key"}))
            == "env-key"
        )

    def test_ollama_returns_empty(self) -> None:
        config = LLMConfig(provider="ollama", api_key=None)
        assert _resolve_api_key(config, Credentials(platform={"openai": "x"})) == ""

    def test_custom_endpoint_never_gets_the_projects_openai_key(self) -> None:
        config = LLMConfig(
            provider="custom_openai", base_url="https://x.test", model="m"
        )
        creds = Credentials(
            platform={"openai": "env"}, project={"openai": {"api_key": "proj"}}
        )
        assert _resolve_api_key(config, creds) == "env"


@pytest.mark.unit
class TestCompleteText:
    @pytest.mark.asyncio
    async def test_successful_completion(self) -> None:
        config = LLMConfig(
            provider="openai",
            model="gpt-4o-mini",
            api_key="test-key",
        )
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]

        mock_response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hi there!"}}],
                "usage": {"total_tokens": 25},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        with patch("turncall.services.llm_text.get_http_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get.return_value = mock_client

            result = await complete_text(config, messages)

        assert isinstance(result, CompletionResult)
        assert result.text == "Hi there!"
        assert result.total_tokens == 25

    @pytest.mark.asyncio
    async def test_reasoning_effort_in_body_when_set(self) -> None:
        config = LLMConfig(
            provider="openai", model="o4-mini", api_key="k", reasoning_effort="high"
        )
        mock_response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 1},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        with patch("turncall.services.llm_text.get_http_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get.return_value = mock_client
            await complete_text(config, [{"role": "user", "content": "hi"}])

        body = mock_client.post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_reasoning_effort_omitted_when_unset(self) -> None:
        config = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="k")
        mock_response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 1},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        with patch("turncall.services.llm_text.get_http_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get.return_value = mock_client
            await complete_text(config, [{"role": "user", "content": "hi"}])

        assert "reasoning_effort" not in mock_client.post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_handles_missing_usage(self) -> None:
        config = LLMConfig(provider="ollama", model="llama3", api_key="test")
        messages = [{"role": "user", "content": "Hi"}]

        mock_response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hello!"}}]},
            request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"),
        )

        with patch("turncall.services.llm_text.get_http_client") as mock_get:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get.return_value = mock_client

            result = await complete_text(config, messages)

        assert result.text == "Hello!"
        assert result.total_tokens == 0
