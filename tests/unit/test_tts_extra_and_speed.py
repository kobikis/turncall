"""`tts.speed` and `tts.extra` reach the TTS provider.

Both fields are declared on `TTSConfig`, and before this neither reached
three of the four providers. `speed` was read only inside the Cartesia
branch, so an agent setting it on the default (Deepgram) voice got nothing;
`extra` was read only for Cartesia's `language` and `emotion`.

Pipecat merges `extra` *over* the declared fields — `given_fields()` does
`result.update(self.extra)` last, and the TTS services have no promotion
step — so an `extra` key naming a setting we manage would silently beat it.
Overflow means "keys we don't manage", so those are dropped.
"""

import pytest

from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _create_tts_service


def _config(**tts_kwargs: object) -> AgentConfig:
    return AgentConfig(system_prompt="hi", tts=TTSConfig(**tts_kwargs))  # type: ignore[arg-type]


@pytest.mark.unit
class TestSpeed:
    def test_deepgram_receives_speed(self) -> None:
        svc = _create_tts_service(_config(speed=1.3), "sk-test")
        assert svc._settings.speed == 1.3

    def test_default_speed_is_not_sent(self) -> None:
        """1.0 is the neutral multiplier and also the default, so leave it unset.

        The service materializes anything we don't pass to `None` (`NOT_GIVEN`
        only lives on the delta), so `None` here means "we sent nothing" —
        identical to the behaviour before `speed` was plumbed at all.
        """
        svc = _create_tts_service(_config(), "sk-test")
        assert svc._settings.speed is None

    def test_openai_receives_speed(self) -> None:
        svc = _create_tts_service(_config(provider="openai", voice="alloy", speed=0.8), "sk-test")
        assert svc._settings.speed == 0.8

    def test_elevenlabs_receives_speed(self) -> None:
        svc = _create_tts_service(_config(provider="elevenlabs", speed=1.1), "sk-test")
        assert svc._settings.speed == 1.1


@pytest.mark.unit
class TestExtra:
    def test_deepgram_extra_reaches_settings(self) -> None:
        svc = _create_tts_service(_config(extra={"made_up_key": "z"}), "sk-test")
        assert svc._settings.given_fields()["made_up_key"] == "z"

    def test_extra_cannot_override_a_managed_field(self) -> None:
        """Pipecat merges extra last, so an unfiltered dict would hijack the voice."""
        svc = _create_tts_service(
            _config(voice="aura-2-helena-en", extra={"voice": "HIJACKED"}), "sk-test"
        )
        assert svc._settings.given_fields()["voice"] == "aura-2-helena-en"

    def test_cartesia_extra_excludes_keys_it_already_handles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "sk-cartesia-test")
        svc = _create_tts_service(
            _config(
                provider="cartesia",
                voice="v-123",
                extra={"emotion": "happy", "made_up_key": "z"},
            ),
            "sk-test",
        )
        assert svc._settings.extra == {"made_up_key": "z"}
@pytest.mark.unit
class TestCartesiaDefaultModel:
    def test_empty_model_falls_back_to_sonic_3_6(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "sk-cartesia-test")
        svc = _create_tts_service(_config(provider="cartesia", voice="v-123", model=""), "sk-test")
        assert svc._settings.model == "sonic-3.6"

    def test_deepgram_default_no_longer_leaks_into_other_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`TTSConfig.model`/`.voice` default to a Deepgram Aura voice whatever
        the provider, so an agent that never set them used to send Cartesia,
        OpenAI and ElevenLabs an Aura name. Each provider now falls back to its
        own default instead.
        """
        monkeypatch.setenv("CARTESIA_API_KEY", "sk-cartesia-test")
        svc = _create_tts_service(_config(provider="cartesia", voice="v-123"), "sk-test")
        assert svc._settings.model == "sonic-3.6"

    def test_openai_falls_back_to_its_own_model_and_voice(self) -> None:
        svc = _create_tts_service(_config(provider="openai"), "sk-test")
        assert svc._settings.model == "tts-1"
        assert svc._settings.voice == "alloy"

    def test_elevenlabs_falls_back_to_its_own_model_and_voice(self) -> None:
        svc = _create_tts_service(_config(provider="elevenlabs"), "sk-test")
        assert svc._settings.model == "eleven_flash_v2_5"
        assert svc._settings.voice == "Rachel"

    def test_deepgram_keeps_its_own_default(self) -> None:
        svc = _create_tts_service(_config(), "sk-test")
        assert svc._settings.voice == "aura-2-helena-en"
