"""One `keyterms` field, four provider dialects.

Vocabulary hints — product names, SKUs, surnames — are the cheapest ASR quality
knob there is, and every provider spells them differently: `keyterm` on
Deepgram and Cartesia, `keywords` on OpenAI, `keyterms` on ElevenLabs. Deepgram
splits its own two spellings by model and rejects the wrong one with a 400 at
connect, so a hand-written `extra: {"keyterm": [...]}` on a Nova-2 agent does
not degrade the call, it ends it.
"""

import pytest

from turncall.domain.models import AgentConfig, STTConfig
from turncall.orchestrator.pipeline_factory import _keyterm_kwargs

TERMS = ["Acme", "SKU-42"]


@pytest.mark.unit
class TestTheDialectMapping:
    @pytest.mark.parametrize(
        ("provider", "model", "expected_key"),
        [
            ("deepgram", "nova-3-general", "keyterm"),
            ("deepgram", "nova-3", "keyterm"),
            ("deepgram", "flux-general-en", "keyterm"),
            ("deepgram", "nova-2", "keywords"),
            ("deepgram", "enhanced", "keywords"),
            ("cartesia", "ink-2", "keyterm"),
            ("cartesia", "ink-whisper", "keyterm"),
            ("openai", "gpt-transcribe", "keywords"),
            ("elevenlabs", "scribe_v1", "keyterms"),
        ],
    )
    def test_each_provider_gets_its_own_spelling(
        self, provider: str, model: str, expected_key: str
    ) -> None:
        assert _keyterm_kwargs(provider, model, TERMS) == {expected_key: TERMS}

    def test_deepgram_splits_by_model_because_both_spellings_are_a_400(self) -> None:
        """Verified against the live API (see C8): Nova-3 answers `keywords are
        not supported for Nova-3`, and Nova-2 answers ``keyterm`` is only
        supported for Nova-3 and Flux``. The model picks, never the caller."""
        assert "keyterm" in _keyterm_kwargs("deepgram", "nova-3-general", TERMS)
        assert "keywords" in _keyterm_kwargs("deepgram", "nova-2", TERMS)

    def test_no_keyterms_sends_no_parameter_at_all(self) -> None:
        """An empty list must not become an empty parameter — providers that
        send keyterms as repeated query params would put a blank one on the
        connection URL."""
        assert _keyterm_kwargs("deepgram", "nova-3-general", []) == {}

    def test_an_unknown_provider_is_a_no_op_rather_than_a_guess(self) -> None:
        assert _keyterm_kwargs("whisper-local", "tiny", TERMS) == {}


@pytest.mark.unit
class TestTheSettingsReachTheService:
    @pytest.mark.parametrize(
        ("provider", "model", "expected_key"),
        [
            ("deepgram", "nova-3-general", "keyterm"),
            ("deepgram", "nova-2", "keywords"),
            ("cartesia", "ink-2", "keyterm"),
            ("elevenlabs", "scribe_v1", "keyterms"),
        ],
    )
    async def test_the_built_service_carries_the_keyterms(
        self, provider: str, model: str, expected_key: str, monkeypatch
    ) -> None:
        """The mapping is only worth anything if it survives construction."""
        from turncall.orchestrator.pipeline_factory import _create_stt_service

        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
        monkeypatch.setenv("CARTESIA_API_KEY", "test-key")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

        config = AgentConfig(
            stt=STTConfig(provider=provider, model=model, keyterms=TERMS)
        )
        try:
            service = _create_stt_service(config, openai_api_key="test-key")
            assert getattr(service._settings, expected_key) == TERMS
        finally:
            # ElevenLabs takes the shared aiohttp session; without this the
            # suite prints an unclosed-session warning for it.
            from turncall.adapters.aiohttp_client import close_aiohttp_session

            await close_aiohttp_session()

    async def test_keyterms_beat_a_leftover_extra_of_the_same_name(
        self, monkeypatch
    ) -> None:
        """`extra` is merged over the declared fields by pipecat, so an old
        hand-written `extra: {"keyterm": ...}` would otherwise win — and could
        carry the wrong spelling to a model that 400s on it."""
        from turncall.orchestrator.pipeline_factory import _create_stt_service

        monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")

        config = AgentConfig(
            stt=STTConfig(
                provider="deepgram",
                model="nova-3-general",
                keyterms=TERMS,
                extra={"keyterm": ["stale"], "profanity_filter": True},
            )
        )
        service = _create_stt_service(config, openai_api_key="test-key")

        assert service._settings.keyterm == TERMS
        assert "keyterm" not in service._settings.extra
        # `profanity_filter` still arrives: Deepgram promotes an extra key that
        # names a declared field onto that field, which is the mechanism the
        # factory docstring relies on.
        assert service._settings.profanity_filter is True


@pytest.mark.unit
class TestTheApiBoundary:
    def test_blanks_and_duplicates_are_dropped_in_order(self) -> None:
        """Cartesia spends a 100-term budget in the order it receives them, so
        a duplicate is a wasted slot rather than a harmless repeat."""
        from turncall.api.v1.schemas.agents import STTConfigSchema

        schema = STTConfigSchema(
            keyterms=["Acme", "  ", "SKU-42", "Acme", " Widget ", ""]
        )

        assert schema.keyterms == ["Acme", "SKU-42", "Widget"]

    def test_the_field_survives_api_ingest(self) -> None:
        """A field missing from the schema is dropped silently on the way in —
        the trap AvatarConfigSchema documents."""
        from turncall.api.v1.schemas.agents import STTConfigSchema

        assert "keyterms" in STTConfigSchema(keyterms=TERMS).model_dump()
