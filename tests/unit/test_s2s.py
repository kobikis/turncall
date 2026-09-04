"""Tests for Speech-to-Speech (S2S) pipeline support."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import (
    AgentConfigSchema,
    S2SConfigSchema,
)
from turncall.domain.enums import PipelineMode
from turncall.domain.models import AgentConfig, S2SConfig


@pytest.mark.unit
class TestPipelineModeEnum:
    def test_values(self) -> None:
        assert PipelineMode.CASCADE == "cascade"
        assert PipelineMode.S2S == "s2s"


@pytest.mark.unit
class TestS2SConfigDomain:
    def test_defaults(self) -> None:
        config = S2SConfig()
        assert config.provider == "openai"
        assert config.model == "gpt-realtime-2.1"
        assert config.voice == "alloy"
        assert config.turn_detection == "server_vad"

    def test_custom_values(self) -> None:
        config = S2SConfig(
            provider="openai",
            model="gpt-realtime-2.1",
            voice="coral",
            turn_detection="pipecat_vad",
        )
        assert config.voice == "coral"
        assert config.turn_detection == "pipecat_vad"

    def test_immutability(self) -> None:
        config = S2SConfig()
        with pytest.raises(ValidationError):
            config.voice = "echo"  # type: ignore[misc]


@pytest.mark.unit
class TestAgentConfigPipelineMode:
    def test_default_is_cascade(self) -> None:
        config = AgentConfig()
        assert config.pipeline_mode == "cascade"

    def test_s2s_mode(self) -> None:
        config = AgentConfig(pipeline_mode="s2s")
        assert config.pipeline_mode == "s2s"
        assert config.s2s.provider == "openai"

    def test_s2s_with_custom_config(self) -> None:
        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(voice="shimmer", model="gpt-realtime-2.1"),
        )
        assert config.s2s.voice == "shimmer"


@pytest.mark.unit
class TestS2SConfigSchemaValidation:
    def test_default_is_valid(self) -> None:
        config = S2SConfigSchema()
        assert config.provider == "openai"
        assert config.voice == "alloy"

    def test_valid_openai_voices(self) -> None:
        for voice in (
            "alloy",
            "ash",
            "ballad",
            "coral",
            "echo",
            "sage",
            "shimmer",
            "verse",
        ):
            config = S2SConfigSchema(voice=voice)
            assert config.voice == voice

    def test_invalid_voice_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid OpenAI Realtime voice"):
            S2SConfigSchema(voice="invalid_voice")

    def test_unsupported_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported S2S provider"):
            S2SConfigSchema(provider="anthropic")

    def test_turn_detection_values(self) -> None:
        S2SConfigSchema(turn_detection="server_vad")
        S2SConfigSchema(turn_detection="pipecat_vad")
        with pytest.raises(ValidationError):
            S2SConfigSchema(turn_detection="invalid")

    def test_gateway_base_url_accepted(self) -> None:
        cfg = S2SConfigSchema(base_url="wss://gateway.example/v1/realtime")
        assert cfg.base_url == "wss://gateway.example/v1/realtime"

    def test_base_url_must_be_websocket(self) -> None:
        with pytest.raises(ValidationError, match="ws:// or wss://"):
            S2SConfigSchema(base_url="https://gateway.example/v1/realtime")

    def test_base_url_only_for_openai_provider(self) -> None:
        with pytest.raises(ValidationError, match="only supported for the openai"):
            S2SConfigSchema(provider="google", base_url="wss://gw/v1/realtime")

    def test_gateway_voice_bypasses_openai_allowlist(self) -> None:
        # A gateway routes to Grok etc. with its own voices — not the OpenAI set.
        cfg = S2SConfigSchema(
            base_url="wss://gateway.example/v1/realtime",
            model="xai/grok-voice-think-fast-1.0",
            voice="thunder",  # not an OpenAI voice
        )
        assert cfg.voice == "thunder"

    def test_non_gateway_voice_still_validated(self) -> None:
        # Without a base_url, the OpenAI voice allowlist still applies.
        with pytest.raises(ValidationError, match="Invalid OpenAI Realtime voice"):
            S2SConfigSchema(voice="thunder")

    def test_serialization_roundtrip(self) -> None:
        config = S2SConfigSchema(voice="coral", turn_detection="pipecat_vad")
        data = config.model_dump()
        restored = S2SConfigSchema.model_validate(data)
        assert restored.voice == "coral"
        assert restored.turn_detection == "pipecat_vad"


@pytest.mark.unit
class TestAgentConfigSchemaS2S:
    def test_default_pipeline_mode_is_cascade(self) -> None:
        config = AgentConfigSchema()
        assert config.pipeline_mode == "cascade"

    def test_s2s_pipeline_mode_valid(self) -> None:
        config = AgentConfigSchema(pipeline_mode="s2s")
        assert config.pipeline_mode == "s2s"

    def test_invalid_pipeline_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfigSchema(pipeline_mode="invalid")

    def test_s2s_with_voicemail_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="Voicemail detection is not supported"
        ):
            AgentConfigSchema(
                pipeline_mode="s2s",
                voicemail_detection={"enabled": True},
            )

    def test_s2s_without_voicemail_valid(self) -> None:
        config = AgentConfigSchema(
            pipeline_mode="s2s",
            voicemail_detection={"enabled": False},
        )
        assert config.pipeline_mode == "s2s"

    def test_cascade_with_voicemail_still_valid(self) -> None:
        config = AgentConfigSchema(
            pipeline_mode="cascade",
            voicemail_detection={"enabled": True},
        )
        assert config.voicemail_detection.enabled is True

    def test_s2s_config_in_assistant(self) -> None:
        config = AgentConfigSchema(
            pipeline_mode="s2s",
            s2s={"provider": "openai", "voice": "coral"},
        )
        assert config.s2s.voice == "coral"

    def test_google_provider_valid(self) -> None:
        config = AgentConfigSchema(
            pipeline_mode="s2s",
            s2s={"provider": "google", "voice": "Kore"},
        )
        assert config.s2s.provider == "google"
        assert config.s2s.voice == "Kore"

    def test_google_voice_not_allowlisted(self) -> None:
        # Gemini's native-audio voice set grows per model, so we don't gate it —
        # a voice outside the classic eight is accepted (Gemini validates).
        config = S2SConfigSchema(provider="google", voice="Sulafat")
        assert config.voice == "Sulafat"

    def test_google_valid_voices(self) -> None:
        for voice in (
            "Aoede",
            "Charon",
            "Fenrir",
            "Kore",
            "Leda",
            "Orus",
            "Puck",
            "Zephyr",
        ):
            config = S2SConfigSchema(provider="google", voice=voice)
            assert config.voice == voice


@pytest.mark.unit
class TestS2SServiceFactory:
    def test_create_openai_realtime_service(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            system_prompt="You are helpful.",
            s2s=S2SConfig(voice="alloy"),
        )
        service = create_s2s_service(config, "test-api-key")

        from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

        assert isinstance(service, OpenAIRealtimeLLMService)
        # No base_url → first-party OpenAI endpoint.
        assert service.base_url.startswith("wss://api.openai.com")

    def test_create_with_gateway_base_url(self) -> None:
        # A gateway base_url + provider-prefixed model routes the realtime
        # WebSocket upstream (e.g. Grok via Vercel AI Gateway / LiteLLM).
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(
                provider="openai",
                base_url="wss://gateway.example/v1/realtime",
                model="xai/grok-voice-think-fast-1.0",
                voice="thunder",
            ),
        )
        service = create_s2s_service(config, "gateway-key")

        assert service.base_url.startswith("wss://gateway.example/v1/realtime")
        assert "xai/grok-voice-think-fast-1.0" in service.base_url

    def test_create_with_pipecat_vad(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(voice="shimmer", turn_detection="pipecat_vad"),
        )
        service = create_s2s_service(config, "test-api-key")
        assert service is not None

    def test_create_gemini_live_service(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            system_prompt="You are helpful.",
            s2s=S2SConfig(
                provider="google",
                model="models/gemini-3.1-flash-live-preview",
                voice="Charon",
            ),
        )
        service = create_s2s_service(config, "unused", google_api_key="test-google-key")

        from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

        assert isinstance(service, GeminiLiveLLMService)

    def test_create_gemini_with_pipecat_vad(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(
                provider="google",
                voice="Kore",
                turn_detection="pipecat_vad",
            ),
        )
        service = create_s2s_service(config, "unused", google_api_key="test-key")
        assert service is not None

    def test_unsupported_provider_raises(self) -> None:
        from turncall.orchestrator.s2s_config import create_s2s_service

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="unsupported", voice="any"),
        )
        with pytest.raises(ValueError, match="Unsupported S2S provider"):
            create_s2s_service(config, "unused")


@pytest.mark.unit
class TestS2SPipelineConstruction:
    def test_s2s_pipeline_mode_dispatches(self) -> None:
        """Verify that pipeline_mode='s2s' calls _create_s2s_pipeline."""
        from unittest.mock import patch

        from turncall.orchestrator.pipeline_factory import create_pipeline

        config = AgentConfig(pipeline_mode="s2s")
        mock_transport = MagicMock()
        mock_context = MagicMock()

        with patch(
            "turncall.orchestrator.pipeline_factory._create_s2s_pipeline"
        ) as mock_s2s:
            mock_s2s.return_value = MagicMock()
            create_pipeline(
                config=config,
                transport=mock_transport,
                call_context=mock_context,
                openai_api_key="test-key",
                pipecat_settings=MagicMock(),
            )
            mock_s2s.assert_called_once()

    def test_gateway_base_url_ssrf_gated(self) -> None:
        """A gateway base_url outside the BYOM allowlist is rejected up front."""
        from turncall.config.settings import BYOMSettings
        from turncall.orchestrator.pipeline_factory import _create_s2s_pipeline

        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(
                provider="openai",
                base_url="wss://evil.example/v1/realtime",
                model="xai/grok-voice-think-fast-1.0",
                voice="thunder",
            ),
        )
        byom = BYOMSettings(enabled=True, allowed_url_patterns=["wss://gateway.example/*"])

        with pytest.raises(ValueError, match="not in allowed patterns"):
            _create_s2s_pipeline(
                config,
                MagicMock(),
                MagicMock(),
                "key",
                MagicMock(),
                byom_settings=byom,
            )

    def test_cascade_pipeline_mode_skips_s2s(self) -> None:
        """Verify that pipeline_mode='cascade' does NOT call _create_s2s_pipeline."""
        from unittest.mock import patch

        from turncall.orchestrator.pipeline_factory import create_pipeline

        config = AgentConfig(pipeline_mode="cascade")
        mock_transport = MagicMock()
        mock_context = MagicMock()

        with patch(
            "turncall.orchestrator.pipeline_factory._create_s2s_pipeline"
        ) as mock_s2s:
            try:
                create_pipeline(
                    config=config,
                    transport=mock_transport,
                    call_context=mock_context,
                    openai_api_key="test-key",
                    pipecat_settings=MagicMock(),
                )
            except Exception:  # noqa: S110
                pass  # Cascade may fail without real transport
            mock_s2s.assert_not_called()
