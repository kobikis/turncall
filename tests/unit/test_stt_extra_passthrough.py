"""`stt.extra` reaches the provider.

The field has existed on `STTConfig` since the beginning but was never read,
so anything set there was accepted by the API, persisted, and silently
dropped. These tests pin the plumbing that makes it live.

Pipecat's `ServiceSettings` treats `extra` as overflow: `given_fields()`
merges its entries at the top level, and Deepgram additionally *promotes* a
key that matches a declared field onto that field. That promotion is what
makes `profanity_filter` reachable again after Pipecat 1.9 stopped sending
it by default.
"""

import pytest

from turncall.domain.models import AgentConfig, STTConfig
from turncall.orchestrator.pipeline_factory import _create_stt_service


def _config(**stt_kwargs: object) -> AgentConfig:
    return AgentConfig(system_prompt="hi", stt=STTConfig(**stt_kwargs))  # type: ignore[arg-type]


@pytest.mark.unit
class TestDeepgramExtra:
    def test_declared_field_is_promoted(self) -> None:
        """A key matching a declared setting lands on the field itself."""
        svc = _create_stt_service(_config(extra={"profanity_filter": True}), "sk-test")
        assert svc._settings.profanity_filter is True

    def test_unknown_key_passes_through(self) -> None:
        """A key Pipecat does not declare still reaches the provider."""
        svc = _create_stt_service(_config(extra={"made_up_key": "z"}), "sk-test")
        assert svc._settings.extra == {"made_up_key": "z"}

    def test_extra_does_not_override_explicit_settings(self) -> None:
        """We set punctuate/smart_format explicitly; extra must not win."""
        svc = _create_stt_service(_config(extra={"punctuate": False}), "sk-test")
        assert svc._settings.punctuate is True

    def test_absent_extra_changes_nothing(self) -> None:
        svc = _create_stt_service(_config(), "sk-test")
        assert svc._settings.extra == {}
        assert svc._settings.model == "nova-3-general"


@pytest.mark.unit
class TestOtherProvidersExtra:
    def test_openai_extra_reaches_settings(self) -> None:
        svc = _create_stt_service(
            _config(
                provider="openai", model="gpt-transcribe", extra={"made_up_key": "z"}
            ),
            "sk-test",
        )
        assert svc._settings.given_fields()["made_up_key"] == "z"

    def test_cartesia_extra_reaches_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "sk-cartesia-test")
        svc = _create_stt_service(
            _config(
                provider="cartesia", model="ink-whisper", extra={"made_up_key": "z"}
            ),
            "sk-test",
        )
        assert svc._settings.given_fields()["made_up_key"] == "z"


@pytest.mark.unit
class TestExtraCannotOverrideManagedSettings:
    """Only Deepgram promotes; the rest let `extra` win, so we filter.

    Without the filter, `extra: {"model": ...}` would beat `stt.model` on
    OpenAI, ElevenLabs and Cartesia, because `given_fields()` merges `extra`
    last and those services have no promotion step.
    """

    def test_openai_extra_cannot_override_model(self) -> None:
        svc = _create_stt_service(
            _config(
                provider="openai", model="gpt-transcribe", extra={"model": "HIJACKED"}
            ),
            "sk-test",
        )
        assert svc._settings.given_fields()["model"] == "gpt-transcribe"

    def test_cartesia_extra_cannot_override_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "sk-cartesia-test")
        svc = _create_stt_service(
            _config(
                provider="cartesia", model="ink-whisper", extra={"model": "HIJACKED"}
            ),
            "sk-test",
        )
        assert svc._settings.given_fields()["model"] == "ink-whisper"

    def test_profanity_filter_still_survives_the_filter(self) -> None:
        """The key this PR exists for is not one we manage, so it passes."""
        svc = _create_stt_service(_config(extra={"profanity_filter": True}), "sk-test")
        assert svc._settings.profanity_filter is True
