"""Advisory warnings on an agent write (#148).

Two checks that cannot be refusals and were reaching nobody.

The **respelling** one is a heuristic, so it can only ever warn: #140's
validator rejects a value on notation, which catches `met-FOR-min` on its hyphen
and capitals but not `metformin` or `met for min` — every character there is a
legal IPA symbol. Measured on Deepgram, that is not harmless: `met for min`
produces 25% more audio than the unhinted sentence, so the voice says something
else and the author is told nothing.

The **unreadable markup** one already existed as a log line at pipeline build
(#147) — mid-call, read by nobody who typed the value — while being fully
knowable from `provider` + `model` + `pronunciations` at create.

Neither may 422. `{"papa": "papa"}` is correct IPA in several languages, and an
agent's voice changes long after its pronunciations are written: refusing correct
input breaks a working agent, where accepting a respelling only mispronounces a
word. #95's `scenario_cannot_fail` is the shape this follows.
"""

from typing import Any

import pytest

from turncall.domain.models import AgentConfig, S2SConfig, TTSConfig

pytestmark = pytest.mark.unit

METFORMIN = {"Metformin": "mɛtˈfɔɹmɪn"}


def _codes(config: AgentConfig) -> list[str]:
    from turncall.orchestrator.config_warnings import config_warnings

    return [warning["code"] for warning in config_warnings(config)]


def _warnings_for(**tts: Any) -> list[dict[str, Any]]:
    from turncall.orchestrator.config_warnings import config_warnings

    return config_warnings(AgentConfig(tts=TTSConfig(**tts)))


class TestTheRespellingHeuristic:
    @pytest.mark.parametrize("value", ["metformin", "met for min", "metformin "])
    def test_the_word_typed_back_at_us_warns(self, value: str) -> None:
        """The trap #140 named, in the spellings its validator cannot see. The
        check is the value normalized — lowercased, spaces stripped — equal to
        the key: that is exactly "the author typed the word back"."""
        found = _warnings_for(pronunciations={"Metformin": value})

        assert [w["code"] for w in found] == ["pronunciation_looks_like_spelling"]
        assert "Metformin" in found[0]["message"]
        assert found[0]["words"] == ["Metformin"]

    @pytest.mark.parametrize(
        "pronunciations",
        [
            METFORMIN,
            {"Kigali": "kiˈɡɑli"},
            # Short English words genuinely carry no stress mark and no symbol
            # outside ASCII — the broader "all lowercase ASCII is suspicious"
            # rule would flag these, which is why the check is equality.
            {"see": "si"},
            {"me": "mi"},
            {"New York": "nu ˈjɔɹk"},
        ],
    )
    def test_real_ipa_says_nothing(self, pronunciations: dict[str, str]) -> None:
        assert not _warnings_for(pronunciations=pronunciations)

    def test_the_known_false_positive_is_a_warning_not_a_refusal(self) -> None:
        """`papa` is a legitimate transcription of "papa". The heuristic fires
        and must never do more than that — a validator that refuses correct IPA
        breaks a working agent, where this leaves one word to check."""
        from turncall.api.v1.schemas.agents import TTSConfigSchema

        assert TTSConfigSchema(pronunciations={"papa": "papa"}).pronunciations
        assert _codes(AgentConfig(tts=TTSConfig(pronunciations={"papa": "papa"}))) == [
            "pronunciation_looks_like_spelling"
        ]

    def test_every_suspicious_word_is_named_once(self) -> None:
        found = _warnings_for(
            pronunciations={
                "Metformin": "metformin",
                "Kigali": "kiˈɡɑli",
                "Ibuprofen": "ibuprofen",
            }
        )

        assert found[0]["words"] == ["Metformin", "Ibuprofen"]

    def test_no_pronunciations_says_nothing(self) -> None:
        assert not _warnings_for()


class TestTheUnreadableMarkupCheck:
    def test_an_elevenlabs_model_that_cannot_read_phonemes_warns(self) -> None:
        """#147's build-time log, moved to where the author can see it."""
        found = _warnings_for(
            provider="elevenlabs",
            model="eleven_flash_v2_5",
            voice="v",
            pronunciations=METFORMIN,
        )

        assert [w["code"] for w in found] == ["pronunciations_unreadable"]
        assert "eleven_flash_v2_5" in found[0]["message"]

    def test_the_warning_names_the_resolved_model(self) -> None:
        """`tts.model`'s default is Deepgram's, so an ElevenLabs agent that
        named none must not be told its voice is `aura-2-helena-en` — the same
        trap `_warn_unusable_pronunciations` documents."""
        found = _warnings_for(
            provider="elevenlabs", voice="v", pronunciations=METFORMIN
        )

        assert found, "an unnamed model still resolves to a non-phoneme default"
        assert "aura-2" not in found[0]["message"]
        assert "eleven_flash_v2_5" in found[0]["message"]

    def test_a_phoneme_capable_elevenlabs_model_says_nothing(self) -> None:
        assert not _warnings_for(
            provider="elevenlabs",
            model="eleven_flash_v2",
            voice="v",
            pronunciations=METFORMIN,
        )

    def test_a_provider_with_no_markup_at_all_warns(self) -> None:
        """OpenAI has no pronunciation dialect. Asked of pipecat's own
        classmethod rather than a list of provider names kept here."""
        found = _warnings_for(
            provider="openai", voice="alloy", pronunciations=METFORMIN
        )

        assert [w["code"] for w in found] == ["pronunciations_unreadable"]
        assert "openai" in found[0]["message"]

    def test_deepgram_says_nothing(self) -> None:
        assert not _warnings_for(provider="deepgram", pronunciations=METFORMIN)

    def test_an_s2s_agent_is_told_at_create_not_mid_call(self) -> None:
        """An S2S pipeline has no TTS stage at all, which is knowable the moment
        the config is written."""
        config = AgentConfig(
            pipeline_mode="s2s",
            s2s=S2SConfig(provider="openai"),
            tts=TTSConfig(pronunciations=METFORMIN),
        )

        assert _codes(config) == ["pronunciations_ignored_on_s2s"]

    def test_no_pronunciations_is_never_warned_about(self) -> None:
        """Absence is not a problem to report — on any provider."""
        for provider in ("openai", "deepgram", "elevenlabs", "cartesia"):
            assert not _warnings_for(provider=provider, voice="v")


class TestTheChannel:
    """#95's shape: derived on every read, never stored."""

    def _row(self, config: dict[str, Any]) -> Any:
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from uuid import uuid4

        return SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            name="support",
            environment="production",
            version=1,
            state="draft",
            config_blob=config,
            created_at=datetime.now(UTC),
            published_at=None,
        )

    def test_the_response_carries_the_warnings(self) -> None:
        from turncall.api.v1.schemas.agents import AgentResponse

        response = AgentResponse.from_row(
            self._row({"tts": {"pronunciations": {"Metformin": "metformin"}}})
        )

        assert [w["code"] for w in response.warnings] == [
            "pronunciation_looks_like_spelling"
        ]
        # ...and out through the envelope the API actually returns.
        assert response.model_dump()["warnings"][0]["words"] == ["Metformin"]

    def test_a_clean_agent_carries_an_empty_list(self) -> None:
        from turncall.api.v1.schemas.agents import AgentResponse

        response = AgentResponse.from_row(
            self._row({"tts": {"pronunciations": METFORMIN}})
        )

        assert response.warnings == []

    def test_a_config_that_cannot_be_read_is_not_an_error(self) -> None:
        """A warning is advisory; a stored config this cannot parse must never
        cost the caller their agent read. `coding-style.md` forbids swallowing it
        silently, so it is logged — but the response still comes back."""
        from turncall.api.v1.schemas.agents import AgentResponse

        response = AgentResponse.from_row(self._row({"tts": "not a mapping"}))

        assert response.warnings == []
