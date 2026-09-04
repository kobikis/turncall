"""Tests for assistant config validation and schemas."""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import (
    AgentConfigSchema,
    CreateAgentRequest,
    LLMConfigSchema,
    STTConfigSchema,
    ToolDefinitionSchema,
    TTSConfigSchema,
)


@pytest.mark.unit
class TestSTTConfigValidation:
    def test_default_config_is_valid(self) -> None:
        config = STTConfigSchema()
        assert config.provider == "deepgram"
        assert config.model == "nova-3-general"

    def test_unsupported_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported STT provider"):
            STTConfigSchema(provider="unknown")


@pytest.mark.unit
class TestLLMConfigValidation:
    def test_default_config_is_valid(self) -> None:
        config = LLMConfigSchema()
        assert config.provider == "openai"
        assert config.model == "gpt-4o-mini"

    def test_temperature_bounds(self) -> None:
        LLMConfigSchema(temperature=0.0)
        LLMConfigSchema(temperature=2.0)
        with pytest.raises(ValidationError):
            LLMConfigSchema(temperature=-0.1)
        with pytest.raises(ValidationError):
            LLMConfigSchema(temperature=2.1)

    def test_reasoning_effort_default_none(self) -> None:
        assert LLMConfigSchema().reasoning_effort is None

    def test_reasoning_effort_accepts_enum_values(self) -> None:
        for value in ("minimal", "low", "medium", "high"):
            assert LLMConfigSchema(reasoning_effort=value).reasoning_effort == value

    def test_reasoning_effort_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfigSchema(reasoning_effort="ultra")

    def test_unsupported_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported LLM provider"):
            LLMConfigSchema(provider="cohere")

    def test_anthropic_provider_valid(self) -> None:
        config = LLMConfigSchema(provider="anthropic", model="claude-sonnet-4-20250514")
        assert config.provider == "anthropic"


@pytest.mark.unit
class TestTTSConfigValidation:
    def test_default_config_is_valid(self) -> None:
        config = TTSConfigSchema()
        assert config.provider == "deepgram"
        assert config.voice == "aura-2-helena-en"

    def test_valid_openai_voices(self) -> None:
        for voice in ("alloy", "echo", "fable", "onyx", "nova", "shimmer"):
            config = TTSConfigSchema(provider="openai", voice=voice)
            assert config.voice == voice

    def test_invalid_openai_voice_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid OpenAI voice"):
            TTSConfigSchema(provider="openai", voice="invalid_voice")

    def test_deepgram_voice_accepted(self) -> None:
        config = TTSConfigSchema(provider="deepgram", voice="aura-2-helena-en")
        assert config.voice == "aura-2-helena-en"

    def test_speed_bounds(self) -> None:
        TTSConfigSchema(speed=0.25)
        TTSConfigSchema(speed=4.0)
        with pytest.raises(ValidationError):
            TTSConfigSchema(speed=0.1)
        with pytest.raises(ValidationError):
            TTSConfigSchema(speed=5.0)


@pytest.mark.unit
class TestToolDefinitionValidation:
    def test_valid_tool_with_webhook(self) -> None:
        tool = ToolDefinitionSchema(
            name="lookup_customer",
            description="Look up customer info",
            webhook_url="https://api.example.com/tools/lookup",
        )
        assert tool.name == "lookup_customer"

    def test_builtin_tool_no_webhook_required(self) -> None:
        tool = ToolDefinitionSchema(
            name="transfer_call",
            description="Transfer call to human agent",
        )
        assert tool.webhook_url is None

    def test_custom_tool_requires_webhook(self) -> None:
        with pytest.raises(ValidationError, match="requires a webhook_url"):
            ToolDefinitionSchema(
                name="custom_tool",
                description="A custom tool",
            )

    def test_tool_name_pattern(self) -> None:
        ToolDefinitionSchema(
            name="valid_name_123",
            description="Valid",
            webhook_url="https://example.com",
        )
        with pytest.raises(ValidationError):
            ToolDefinitionSchema(
                name="Invalid-Name",
                description="Invalid",
                webhook_url="https://example.com",
            )

    def test_timeout_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ToolDefinitionSchema(
                name="test_tool",
                description="Test",
                webhook_url="https://example.com",
                timeout_seconds=0,
            )
        with pytest.raises(ValidationError):
            ToolDefinitionSchema(
                name="test_tool",
                description="Test",
                webhook_url="https://example.com",
                timeout_seconds=301,
            )


@pytest.mark.unit
class TestAgentConfigValidation:
    def test_default_config_is_valid(self) -> None:
        config = AgentConfigSchema()
        assert config.silence_timeout_ms == 800
        assert config.interruption_enabled is True

    def test_duplicate_tool_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            AgentConfigSchema(
                tools=[
                    ToolDefinitionSchema(
                        name="transfer_call",
                        description="Transfer 1",
                    ),
                    ToolDefinitionSchema(
                        name="transfer_call",
                        description="Transfer 2",
                    ),
                ]
            )

    def test_silence_timeout_bounds(self) -> None:
        AgentConfigSchema(silence_timeout_ms=200)
        AgentConfigSchema(silence_timeout_ms=5000)
        with pytest.raises(ValidationError):
            AgentConfigSchema(silence_timeout_ms=100)

    def test_system_prompt_max_length(self) -> None:
        AgentConfigSchema(system_prompt="x" * 128000)
        with pytest.raises(ValidationError):
            AgentConfigSchema(system_prompt="x" * 128001)

    def test_config_serialization_roundtrip(self) -> None:
        config = AgentConfigSchema(
            system_prompt="You are a helpful assistant.",
            first_message="Hello! How can I help?",
            language="en",
            tools=[
                ToolDefinitionSchema(
                    name="end_call",
                    description="End the call",
                ),
            ],
        )
        data = config.model_dump()
        restored = AgentConfigSchema.model_validate(data)
        assert restored.system_prompt == config.system_prompt
        assert len(restored.tools) == 1


@pytest.mark.unit
class TestCreateAgentRequest:
    def test_valid_request(self) -> None:
        req = CreateAgentRequest(name="my-assistant")
        assert req.environment == "development"

    def test_environment_validation(self) -> None:
        CreateAgentRequest(name="test", environment="production")
        with pytest.raises(ValidationError):
            CreateAgentRequest(name="test", environment="invalid")

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            CreateAgentRequest()  # type: ignore[call-arg]
