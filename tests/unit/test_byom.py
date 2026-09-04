"""Tests for Bring Your Own Model (BYOM) support."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import (
    AgentResponse,
    LLMConfigSchema,
)
from turncall.config.settings import BYOMSettings
from turncall.domain.models import AgentConfig, LLMConfig


@pytest.mark.unit
class TestLLMConfigDomain:
    def test_default_has_no_base_url(self) -> None:
        config = LLMConfig()
        assert config.base_url is None
        assert config.api_key is None

    def test_with_base_url(self) -> None:
        config = LLMConfig(
            provider="ollama",
            model="gemma4:12b",
            base_url="http://localhost:11434/v1",
        )
        assert config.base_url == "http://localhost:11434/v1"

    def test_immutability(self) -> None:
        config = LLMConfig(provider="ollama", base_url="http://localhost:11434/v1")
        with pytest.raises(ValidationError):
            config.base_url = "http://other:11434/v1"  # type: ignore[misc]


@pytest.mark.unit
class TestLLMConfigSchemaValidation:
    def test_openai_default_still_valid(self) -> None:
        config = LLMConfigSchema()
        assert config.provider == "openai"
        assert config.base_url is None
        assert config.api_key is None

    def test_ollama_provider_valid(self) -> None:
        config = LLMConfigSchema(provider="ollama", model="gemma4:12b")
        assert config.provider == "ollama"
        assert config.model == "gemma4:12b"

    def test_ollama_with_base_url(self) -> None:
        config = LLMConfigSchema(
            provider="ollama",
            model="llama3:8b",
            base_url="http://192.168.1.100:11434/v1",
        )
        assert config.base_url == "http://192.168.1.100:11434/v1"

    def test_custom_openai_requires_base_url(self) -> None:
        with pytest.raises(ValidationError, match="base_url is required"):
            LLMConfigSchema(provider="custom_openai", model="llama3")

    def test_custom_openai_with_base_url_valid(self) -> None:
        config = LLMConfigSchema(
            provider="custom_openai",
            model="meta-llama/Llama-3-70b",
            base_url="https://api.together.xyz/v1",
            api_key="tok_abc123",
        )
        assert config.provider == "custom_openai"
        assert config.base_url == "https://api.together.xyz/v1"
        assert config.api_key == "tok_abc123"

    def test_invalid_base_url_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must start with http"):
            LLMConfigSchema(
                provider="custom_openai",
                model="test",
                base_url="ftp://bad.url/v1",
            )

    def test_openai_ignores_base_url(self) -> None:
        config = LLMConfigSchema(
            provider="openai",
            base_url="http://localhost:11434/v1",
        )
        assert config.provider == "openai"
        assert config.base_url == "http://localhost:11434/v1"

    def test_unsupported_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported LLM provider"):
            LLMConfigSchema(provider="cohere")

    def test_serialization_roundtrip(self) -> None:
        config = LLMConfigSchema(
            provider="ollama",
            model="gemma4:12b",
            base_url="http://localhost:11434/v1",
        )
        data = config.model_dump()
        restored = LLMConfigSchema.model_validate(data)
        assert restored.provider == config.provider
        assert restored.base_url == config.base_url


@pytest.mark.unit
class TestAgentResponseSanitization:
    def test_api_key_stripped_from_response(self) -> None:
        row = MagicMock()
        row.id = "00000000-0000-0000-0000-000000000001"
        row.project_id = "00000000-0000-0000-0000-000000000002"
        row.name = "test"
        row.environment = "development"
        row.version = 1
        row.state = "draft"
        row.config_blob = {
            "llm": {
                "provider": "custom_openai",
                "model": "test",
                "base_url": "https://api.together.xyz/v1",
                "api_key": "secret-key-123",
            }
        }
        row.created_at = "2026-01-01T00:00:00Z"
        row.published_at = None

        response = AgentResponse.from_row(row)
        assert response.config["llm"]["api_key"] == "***"
        assert response.config["llm"]["base_url"] == "https://api.together.xyz/v1"

    def test_no_api_key_unchanged(self) -> None:
        row = MagicMock()
        row.id = "00000000-0000-0000-0000-000000000001"
        row.project_id = "00000000-0000-0000-0000-000000000002"
        row.name = "test"
        row.environment = "development"
        row.version = 1
        row.state = "draft"
        row.config_blob = {"llm": {"provider": "openai", "model": "gpt-4o-mini"}}
        row.created_at = "2026-01-01T00:00:00Z"
        row.published_at = None

        response = AgentResponse.from_row(row)
        assert "api_key" not in response.config["llm"]


@pytest.mark.unit
class TestBYOMSettings:
    def test_defaults(self) -> None:
        settings = BYOMSettings()
        assert settings.enabled is True
        assert settings.allowed_url_patterns == []

    def test_with_patterns(self) -> None:
        settings = BYOMSettings(
            allowed_url_patterns=["http://localhost:*", "https://*.together.xyz/*"]
        )
        assert len(settings.allowed_url_patterns) == 2


@pytest.mark.unit
class TestPipelineFactoryBYOM:
    def test_create_llm_service_openai(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(llm=LLMConfig(provider="openai", model="gpt-4o-mini"))
        service = _create_llm_service(config, "test-key")
        from pipecat.services.openai.llm import OpenAILLMService

        assert isinstance(service, OpenAILLMService)

    def test_create_llm_service_ollama(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(llm=LLMConfig(provider="ollama", model="gemma4:12b"))
        service = _create_llm_service(config, "unused")
        from pipecat.services.ollama.llm import OLLamaLLMService

        assert isinstance(service, OLLamaLLMService)

    def test_create_llm_service_custom_openai(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(
            llm=LLMConfig(
                provider="custom_openai",
                model="llama3",
                base_url="https://api.together.xyz/v1",
                api_key="tok_123",
            )
        )
        service = _create_llm_service(config, "unused")
        from pipecat.services.openai.llm import OpenAILLMService

        assert isinstance(service, OpenAILLMService)

    def test_create_llm_service_unsupported(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(llm=LLMConfig(provider="cohere", model="command-r"))
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            _create_llm_service(config, "unused")

    def test_create_llm_service_anthropic(self) -> None:
        from pipecat.services.anthropic.llm import AnthropicLLMService

        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(
            llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-20250514")
        )
        service = _create_llm_service(config, "unused", anthropic_api_key="test-key")
        assert isinstance(service, AnthropicLLMService)

    def test_custom_openai_requires_base_url(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        config = AgentConfig(llm=LLMConfig(provider="custom_openai", model="test"))
        with pytest.raises(ValueError, match="base_url is required"):
            _create_llm_service(config, "unused")

    def test_byom_url_allowlist_blocks_disallowed(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        byom = BYOMSettings(allowed_url_patterns=["http://localhost:*"])
        config = AgentConfig(
            llm=LLMConfig(
                provider="custom_openai",
                model="test",
                base_url="https://evil.com/v1",
            )
        )
        with pytest.raises(ValueError, match="not in allowed patterns"):
            _create_llm_service(config, "unused", byom_settings=byom)

    def test_byom_url_allowlist_allows_matching(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        byom = BYOMSettings(
            allowed_url_patterns=["http://localhost:*", "https://*.together.xyz/*"]
        )
        config = AgentConfig(
            llm=LLMConfig(
                provider="ollama",
                model="gemma4:12b",
                base_url="http://localhost:11434/v1",
            )
        )
        service = _create_llm_service(config, "unused", byom_settings=byom)
        assert service is not None

    def test_byom_disabled_rejects_all(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        byom = BYOMSettings(enabled=False)
        config = AgentConfig(llm=LLMConfig(provider="ollama", model="gemma4:12b"))
        with pytest.raises(ValueError, match="disabled"):
            _create_llm_service(config, "unused", byom_settings=byom)

    def test_empty_allowlist_allows_all(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_llm_service

        byom = BYOMSettings(allowed_url_patterns=[])
        config = AgentConfig(
            llm=LLMConfig(
                provider="custom_openai",
                model="test",
                base_url="https://anywhere.com/v1",
                api_key="key",
            )
        )
        service = _create_llm_service(config, "unused", byom_settings=byom)
        assert service is not None


@pytest.mark.unit
class TestToolBridgeGenerics:
    def test_register_function_on_base_llm_service(self) -> None:
        """Verify register_tools accepts any LLMService, not just OpenAILLMService."""
        from turncall.orchestrator.tool_bridge import register_tools

        mock_llm = MagicMock()
        mock_llm.register_function = MagicMock()

        mock_context = MagicMock()
        mock_context.call_id = "00000000-0000-0000-0000-000000000001"
        mock_context.project_id = "00000000-0000-0000-0000-000000000002"
        mock_context.session_factory = MagicMock()

        from turncall.domain.models import ToolDefinition

        tools = [
            ToolDefinition(
                name="end_call",
                description="End the call",
                parameters_schema={},
            )
        ]

        register_tools(mock_llm, tools, mock_context)
        mock_llm.register_function.assert_called_once()
        call_args = mock_llm.register_function.call_args
        assert call_args[0][0] == "end_call"
