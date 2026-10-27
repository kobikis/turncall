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
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger

from tests.conftest import creds
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
        assert len(transforms) == 3, transforms

    @pytest.mark.parametrize("service_cls", _tts_service_classes())
    async def test_the_word_reaches_the_service_as_that_providers_markup(
        self, service_cls: type
    ) -> None:
        """One IPA spelling, three dialects. What each looks like is the
        vendor's business; that the word stops being plain text is ours."""
        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        *_, (_, transform) = _tts_text_transforms(
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

        *_, (_, transform) = _tts_text_transforms(
            OpenAITTSService, AgentConfig(tts=TTSConfig(pronunciations=METFORMIN))
        )

        assert await transform("Take your Metformin", "*") == "Take your Metformin"

    async def test_no_pronunciations_adds_no_transform(self) -> None:
        """An empty map is the field being absent, in the pipeline too."""
        from pipecat.services.cartesia.tts import CartesiaTTSService

        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        assert len(_tts_text_transforms(CartesiaTTSService, AgentConfig())) == 2


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
        tts = _create_tts_service(config, credentials=creds("x"))

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
        tts = _create_tts_service(config, credentials=creds("x"))

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
        tts = _create_tts_service(config, credentials=creds("x"))

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
        tts = _create_tts_service(config, credentials=creds("x"))

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
                pipecat_settings=MagicMock(),
                credentials=creds(""),
            )
        )

        assert any("tts.pronunciations is ignored on S2S" in m for m in said), said


@pytest.mark.unit
class TestElevenLabsSsmlParsing:
    """ElevenLabs reads the markup only with SSML parsing on (#147).

    `enable_ssml_parsing` is a **constructor kwarg**, not a field on
    `ElevenLabsTTSService.Settings`, so an author's `tts.extra` landed in
    `Settings.extra` and never reached it — the warning above was correct with
    no configuration path out of it. TurnCall decides instead, scoped to agents
    that asked for pronunciations: SSML parsing changes how *all* of that
    agent's text is read (a `<` in an LLM reply becomes markup) and is reported
    to add latency, so it is not a default.

    The model half stays the author's. `eleven_flash_v2_5` is a better voice
    than `eleven_flash_v2`, and downgrading it to make a pronunciation work
    trades what every caller hears for what one might; the warning naming the
    resolved model is the nudge.
    """

    def _built(self, **tts: object) -> tuple[AgentConfig, Any]:
        """The config and the service built from it — one source of truth, since
        the warning is asked of both."""
        from turncall.orchestrator.pipeline_factory import _create_tts_service

        config = AgentConfig(
            tts=TTSConfig(provider="elevenlabs", voice="21m00Tcm4TlvDq8ikWAM", **tts)
        )
        return config, _create_tts_service(config, credentials=creds("x"))

    def test_a_phoneme_model_with_pronunciations_reads_the_markup(self) -> None:
        """The whole point: asked of the *constructed* service, which is where
        the measurement in #147 said False."""
        from turncall.orchestrator.pipeline_factory import (
            _warn_unusable_pronunciations,
        )

        config, tts = self._built(model="eleven_flash_v2", pronunciations=METFORMIN)

        assert tts.supports_pronunciations
        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))
        assert not [m for m in said if "pronunciation" in m], said

    async def test_the_word_reaches_that_service_as_phoneme_markup(self) -> None:
        """Reading SSML is no use if the transform is not building it."""
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        from turncall.orchestrator.pipeline_factory import _tts_text_transforms

        *_, (_, transform) = _tts_text_transforms(
            ElevenLabsTTSService,
            AgentConfig(tts=TTSConfig(pronunciations=METFORMIN)),
        )

        assert "<phoneme" in await transform("Take your Metformin", "*")

    def test_an_agent_with_no_pronunciations_is_built_as_it_was(self) -> None:
        """SSML parsing is not turned on for text that never asked for it —
        every `<` in every LLM reply would start meaning something."""
        _, tts = self._built(model="eleven_flash_v2")

        assert tts._enable_ssml_parsing is None

    def test_a_model_that_cannot_read_phonemes_still_warns(self) -> None:
        """The resolved default. Turning SSML parsing on would not make this
        model read the tags, so the warning stays and still names the model."""
        from turncall.orchestrator.pipeline_factory import (
            _warn_unusable_pronunciations,
        )

        config, tts = self._built(model="eleven_flash_v2_5", pronunciations=METFORMIN)

        assert not tts.supports_pronunciations
        said = _warnings(lambda: _warn_unusable_pronunciations(config, tts))
        assert any("eleven_flash_v2_5" in m for m in said), said

    async def test_the_markup_is_withheld_where_it_would_be_read_aloud(self) -> None:
        """Measured on the WebSocket pipecat uses: a `<phoneme>` tag reaching a
        service that is not parsing SSML is **spoken**, at 3.5-4.3x the audio of
        the plain sentence — "phoneme alphabet i p a" read out to the caller. So
        the transform is withheld when the built service will not read the markup,
        which is what "spoken as written" has to mean here. The warning above is
        what tells the author; this is what the caller hears meanwhile.
        """
        _, tts = self._built(model="eleven_flash_v2_5", pronunciations=METFORMIN)

        spoken = "Take your Metformin today"
        for _, transform in tts._text_transforms:
            spoken = await transform(spoken, "*")

        assert spoken == "Take your Metformin today"

    async def test_a_capable_model_still_gets_the_markup(self) -> None:
        """The other side of that gate, asked of the constructed service."""
        _, tts = self._built(model="eleven_flash_v2", pronunciations=METFORMIN)

        spoken = "Take your Metformin today"
        for _, transform in tts._text_transforms:
            spoken = await transform(spoken, "*")

        assert "<phoneme" in spoken

    def test_the_phoneme_model_pin_is_pipecats_own(self) -> None:
        """Not a second copy of the frozenset: every model pipecat lists reads
        the markup on a service this factory built. A pipecat bump that renames
        or moves `ELEVENLABS_PHONEME_MODELS` is then a red build naming the
        version, the way `test_pipecat_parser_contract.py` is for the eval
        parser — not an ElevenLabs agent that silently stopped reading IPA.
        """
        from importlib.metadata import version

        from pipecat.services.elevenlabs.tts import ELEVENLABS_PHONEME_MODELS

        why = (
            f"pipecat {version('pipecat-ai')} moved or emptied "
            "ELEVENLABS_PHONEME_MODELS; the factory keys SSML parsing off it, so "
            "ElevenLabs pronunciations go with it (#147)."
        )
        assert ELEVENLABS_PHONEME_MODELS, why
        for model in ELEVENLABS_PHONEME_MODELS:
            _, tts = self._built(model=model, pronunciations=METFORMIN)
            assert tts.supports_pronunciations, f"{model}: {why}"
