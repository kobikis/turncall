"""`STTConfig.model` used to hold one provider's model for all of them.

The default was `nova-3-general` — Deepgram's — in a provider-agnostic field,
and the `or "<default>"` fallbacks each branch carried could never fire
against a truthy value. An agent that named no model therefore sent Deepgram's
model name to whichever provider it had picked. The other three reject it:

    openai      404 The model `nova-3-general` does not exist
    elevenlabs  400 unsupported_model: 'nova-3-general' is not a valid model
    cartesia    400 invalid model

So the STT stage never connected and the caller was heard by nothing. TTS has
had `_tts_model_voice()` for this since it was written; STT never did.
"""

import pytest

from turncall.domain.models import AgentConfig, STTConfig
from turncall.orchestrator.pipeline_factory import _stt_model


@pytest.mark.unit
class TestResolvingAnUnsetModel:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("deepgram", "nova-3-general"),
            ("openai", "gpt-transcribe"),
            ("elevenlabs", "scribe_v1"),
            ("cartesia", "ink-whisper"),
        ],
    )
    def test_each_provider_gets_its_own_default(
        self, provider: str, expected: str
    ) -> None:
        assert _stt_model(provider, "") == expected

    def test_an_explicit_model_is_passed_through(self) -> None:
        """A wrong model the agent actually chose is reported by the provider
        rather than silently replaced — adr/0016's rule."""
        assert _stt_model("openai", "whisper-1") == "whisper-1"
        assert _stt_model("elevenlabs", "scribe_v2") == "scribe_v2"

    def test_an_unknown_provider_keeps_whatever_it_was_given(self) -> None:
        assert _stt_model("whisper-local", "tiny") == "tiny"
        assert _stt_model("whisper-local", "") == ""


@pytest.mark.unit
class TestTheLegacySentinel:
    """`model_dump()` persists the whole config, so every agent created before
    this fix has `"model": "nova-3-general"` written into its config_blob. The
    value has to be read as "unset" on the providers that cannot serve it, or
    those agents stay broken until someone edits them by hand.
    """

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("openai", "gpt-transcribe"),
            ("elevenlabs", "scribe_v1"),
            ("cartesia", "ink-whisper"),
        ],
    )
    def test_a_stored_deepgram_model_heals_on_other_providers(
        self, provider: str, expected: str
    ) -> None:
        assert _stt_model(provider, "nova-3-general") == expected

    def test_deepgram_keeps_it_because_there_it_is_correct(self) -> None:
        assert _stt_model("deepgram", "nova-3-general") == "nova-3-general"


@pytest.mark.unit
class TestTheServiceGetsTheResolvedModel:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("deepgram", "nova-3-general"),
            ("elevenlabs", "scribe_v1"),
            ("cartesia", "ink-whisper"),
            ("openai", "gpt-transcribe"),
        ],
    )
    async def test_an_agent_that_named_no_model_still_builds_a_usable_one(
        self, provider: str, expected: str, monkeypatch
    ) -> None:
        from turncall.orchestrator.pipeline_factory import _create_stt_service

        for key in ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY"):
            monkeypatch.setenv(key, "test-key")

        config = AgentConfig(stt=STTConfig(provider=provider))
        try:
            service = _create_stt_service(config, openai_api_key="test-key")
            assert service._settings.model == expected
        finally:
            from turncall.adapters.aiohttp_client import close_aiohttp_session

            await close_aiohttp_session()

    async def test_keyterms_follow_the_resolved_model_not_the_blank_one(
        self, monkeypatch
    ) -> None:
        """Deepgram's keyterm/keywords split is decided by the model, so the
        mapping has to see the resolved one — a blank would have fallen to the
        `keywords` branch and 400'd on Nova-3."""
        from turncall.orchestrator.pipeline_factory import _create_stt_service

        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")

        config = AgentConfig(stt=STTConfig(provider="deepgram", keyterms=["Acme"]))
        service = _create_stt_service(config, openai_api_key="test-key")

        assert service._settings.keyterm == ["Acme"]
