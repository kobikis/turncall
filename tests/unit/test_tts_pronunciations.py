"""One `tts.pronunciations` dictionary, four provider dialects (#140).

The outbound mirror of `stt.keyterms`: one per-agent vocabulary hint, cleaned
up at the same API boundary, mapped to each provider's own markup one stage
later. `test_stt_keyterms.py` is the shape this follows.

The validator is TurnCall's own and this is the part most likely to be got
wrong. Pipecat's IPA utilities normalize and tokenize but never reject —
measured against 1.12, `ipa_phones("met-FOR-min")` happily returns ten tokens.
So a respelled value would be accepted, inserted into the markup and change
nothing audible. Hence a 422 here rather than "pipecat will warn about it",
which it only does when the transform is built: on a live call, at connect,
with a caller already listening.
"""

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from loguru import logger

from turncall.domain.models import AgentConfig, S2SConfig, TTSConfig

METFORMIN = {"Metformin": "mɛtˈfɔɹmɪn"}


def _warnings(call: Callable[[], object]) -> list[str]:
    """Loguru does not reach caplog; test_interruption_enabled.py's idiom."""
    said: list[str] = []
    sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
    try:
        call()
    finally:
        logger.remove(sink)
    return said


def _tts_service_classes() -> list[type]:
    from pipecat.services.cartesia.tts import CartesiaTTSService
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    # OpenAI is deliberately absent: it has no pronunciation markup at all, and
    # is the warning case tested below rather than a dialect.
    return [DeepgramTTSService, ElevenLabsTTSService, CartesiaTTSService]


@pytest.mark.unit
class TestTheApiBoundary:
    def test_respelled_ascii_is_rejected_naming_the_word(self) -> None:
        """What someone actually types first. Accepted today, it would reach the
        markup and do nothing."""
        from turncall.api.v1.schemas.agents import TTSConfigSchema

        with pytest.raises(ValueError, match="Metformin"):
            TTSConfigSchema(pronunciations={"Metformin": "met-FOR-min"})

    @pytest.mark.parametrize("value", ["not ipa at all!", "xyz123", "MƐT"])
    def test_pipecats_own_tokenizer_accepts_what_this_rejects(self, value: str) -> None:
        """The reason the validator exists, asserted against the installed
        pipecat rather than taken on trust — if a future version starts
        rejecting these, this file is the place that says so."""
        from pipecat.utils.text.phonemes import ipa_phones

        from turncall.api.v1.schemas.agents import TTSConfigSchema

        assert ipa_phones(value), "pipecat rejected it after all"
        with pytest.raises(ValueError):
            TTSConfigSchema(pronunciations={"word": value})

    def test_delimiters_are_stripped_and_notation_canonicalized(self) -> None:
        """Two spellings of one transcription must be one stored config, so the
        stored form is pipecat's canonical one: no /…/ or […], `ʧ` written
        `tʃ`, `'` written `ˈ`, syllable breaks dropped."""
        from turncall.api.v1.schemas.agents import TTSConfigSchema

        schema = TTSConfigSchema(
            pronunciations={
                "Kigali": "/kiˈɡɑli/",
                "Chicken": "[ˈʧɪ.kən]",
                "Nurse": "'nɜrs",
            }
        )

        assert schema.pronunciations == {
            "Kigali": "kiˈɡɑli",
            "Chicken": "ˈtʃɪkən",
            "Nurse": "ˈnɜrs",
        }
        # Idempotent: storing then re-ingesting must not change it again.
        assert (
            TTSConfigSchema(pronunciations=schema.pronunciations).pronunciations
            == schema.pronunciations
        )

    def test_blanks_are_dropped_and_an_empty_map_is_absence(self) -> None:
        """An empty map has to be exactly equivalent to the field being absent,
        the same rule blank keyterms follow."""
        from turncall.api.v1.schemas.agents import TTSConfigSchema

        schema = TTSConfigSchema(
            pronunciations={"  ": "mɛt", "Metformin": "mɛtˈfɔɹmɪn", "Blank": "  "}
        )

        assert schema.pronunciations == METFORMIN
        assert TTSConfigSchema(pronunciations={}).pronunciations == {}
        assert TTSConfigSchema().pronunciations == {}

    def test_the_field_survives_api_ingest(self) -> None:
        """A field missing from the schema is dropped silently on the way in —
        the trap AvatarConfigSchema documents."""
        from turncall.api.v1.schemas.agents import TTSConfigSchema

        dumped = TTSConfigSchema(pronunciations=METFORMIN).model_dump()

        assert dumped["pronunciations"] == METFORMIN
        # ...and back out through the domain model, which is what create, read,
        # publish and rollback all round-trip through (`config_blob`).
        assert (
            AgentConfig.model_validate({"tts": dumped}).tts.pronunciations == METFORMIN
        )


@pytest.mark.unit
class TestThePipeline:
    @pytest.mark.parametrize("service_cls", _tts_service_classes())
    async def test_the_pronunciation_transform_is_registered_last(
        self, service_cls: type
    ) -> None:
        """Transforms run in order and a later one rewrites what an earlier one
        inserted — markdown stripping would eat an SSML <phoneme> tag."""
        from pipecat.utils.text.transforms.pronunciations import PronunciationTransform

        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        transforms = _tts_text_transforms(
            service_cls, AgentConfig(tts=TTSConfig(pronunciations=METFORMIN))
        )

        assert isinstance(transforms[-1][1], PronunciationTransform)
        assert len(transforms) == 2, transforms

    @pytest.mark.parametrize("service_cls", _tts_service_classes())
    async def test_the_word_reaches_the_service_as_that_providers_markup(
        self, service_cls: type
    ) -> None:
        """One IPA spelling, three dialects. What each looks like is the
        vendor's business; that the word stops being plain text is ours."""
        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        _, (_, transform) = _tts_text_transforms(
            service_cls, AgentConfig(tts=TTSConfig(pronunciations=METFORMIN))
        )

        spoken = await transform("Take your Metformin today", "*")

        assert spoken != "Take your Metformin today"
        assert "today" in spoken

    async def test_one_unusable_word_never_silences_the_reply(self) -> None:
        """A provider that cannot render a word leaves it as written rather than
        producing no text — losing a word is a mispronunciation, losing the
        text is a dead call."""
        from pipecat.services.openai.tts import OpenAITTSService

        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        _, (_, transform) = _tts_text_transforms(
            OpenAITTSService, AgentConfig(tts=TTSConfig(pronunciations=METFORMIN))
        )

        assert await transform("Take your Metformin", "*") == "Take your Metformin"

    async def test_no_pronunciations_adds_no_transform(self) -> None:
        """An empty map is the field being absent, in the pipeline too."""
        from pipecat.services.cartesia.tts import CartesiaTTSService

        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        assert len(_tts_text_transforms(CartesiaTTSService, AgentConfig())) == 1


@pytest.mark.unit
class TestTheWarnings:
    def test_a_voice_that_cannot_read_markup_warns_and_proceeds(self) -> None:
        """Capability belongs to the *constructed* service, not the provider
        name: OpenAI has no markup, and ElevenLabs needs both a phoneme model
        and SSML parsing. Never a refusal at create — an agent's voice can
        change long after its pronunciations are written."""
        from turncall.orchestrator.pipeline_factory import (
            _create_tts_service,
            _warn_unusable_pronunciations,
        )

        config = AgentConfig(
            tts=TTSConfig(provider="openai", voice="alloy", pronunciations=METFORMIN)
        )
        tts = _create_tts_service(config, openai_api_key="x")

        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))

        assert any(
            "openai" in m and "Metformin" in m and "spoken as written" in m
            for m in said
        ), said

    def test_the_check_is_the_service_not_the_provider_name(self) -> None:
        """ElevenLabs renders SSML <phoneme> tags, so the provider name says
        "supported" — but the built service only reads them with a
        phoneme-capable model and SSML parsing on, neither of which a provider
        name knows. This is the case a create-time check would get wrong."""
        from turncall.orchestrator.pipeline_factory import (
            _create_tts_service,
            _warn_unusable_pronunciations,
        )

        config = AgentConfig(
            tts=TTSConfig(
                provider="elevenlabs",
                model="eleven_flash_v2_5",
                voice="21m00Tcm4TlvDq8ikWAM",
                pronunciations=METFORMIN,
            )
        )
        tts = _create_tts_service(config, openai_api_key="x")

        assert tts.format_pronunciation("Metformin", "mɛtˈfɔɹmɪn") is not None
        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))

        assert any("eleven_flash_v2_5" in m for m in said), said

    def test_the_warning_names_the_model_the_service_actually_runs(self) -> None:
        """`tts.model`'s default is Deepgram's, so an ElevenLabs agent that
        named no model would be told its voice is `aura-2-helena-en`. A
        diagnostic pointing at a model the service is not running sends the
        reader to the wrong place — and the model is half of why it refused."""
        from turncall.orchestrator.pipeline_factory import (
            _create_tts_service,
            _tts_model_voice,
            _warn_unusable_pronunciations,
        )

        config = AgentConfig(
            tts=TTSConfig(provider="elevenlabs", voice="v", pronunciations=METFORMIN)
        )
        assert config.tts.model == "aura-2-helena-en", "the sentinel moved"
        tts = _create_tts_service(config, openai_api_key="x")

        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))

        resolved = _tts_model_voice(config)[0]
        assert resolved == tts._settings.model, "resolution changed underneath"
        assert any(resolved in m for m in said), said
        assert not [m for m in said if "aura-2" in m], said

    def test_a_voice_that_can_read_markup_says_nothing(self) -> None:
        from turncall.orchestrator.pipeline_factory import (
            _create_tts_service,
            _warn_unusable_pronunciations,
        )

        config = AgentConfig(
            tts=TTSConfig(provider="deepgram", pronunciations=METFORMIN)
        )
        tts = _create_tts_service(config, openai_api_key="x")

        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))

        assert not [m for m in said if "pronunciation" in m], said

    def test_an_s2s_agent_is_warned_rather_than_ignored(self) -> None:
        """No TTS stage to attach the transform to. Same precedent as
        interruption_enabled on S2S: said out loud rather than half-applied,
        because the setting was accepted, stored and read."""
        from turncall.orchestrator.pipeline_factory import _create_s2s_pipeline

        said = _warnings(
            lambda: _create_s2s_pipeline(
                config=AgentConfig(
                    pipeline_mode="s2s",
                    s2s=S2SConfig(provider="openai"),
                    tts=TTSConfig(pronunciations=METFORMIN),
                ),
                transport=MagicMock(),
                call_context=MagicMock(),
                openai_api_key="",
                pipecat_settings=MagicMock(),
            )
        )

        assert any("tts.pronunciations is ignored on S2S" in m for m in said), said
