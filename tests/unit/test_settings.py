"""Tests for settings configuration."""

import pytest

from turncall.config.settings import Environment, Settings


@pytest.mark.unit
class TestSettings:
    def test_default_environment(self) -> None:
        settings = Settings()
        assert settings.environment == Environment.DEVELOPMENT

    def test_is_production_false_by_default(self) -> None:
        settings = Settings()
        assert settings.is_production is False

    def test_database_defaults(self) -> None:
        settings = Settings()
        assert settings.database.pool_size == 20
        assert settings.database.max_overflow == 10

    def test_openai_defaults(self) -> None:
        settings = Settings()
        assert settings.openai.stt_model == "whisper-1"
        assert settings.openai.llm_model == "gpt-4o"
        assert settings.openai.tts_model == "tts-1"

    def test_storage_defaults_to_local(self) -> None:
        settings = Settings()
        assert settings.storage.backend == "local"


@pytest.mark.unit
class TestWebhookSecrets:
    """A webhook-signing secret that is only whitespace is unset, for every
    reader (#337, #339): otherwise it is a key anyone can sign with, and the
    refusal of unverifiable webhooks never runs."""

    @pytest.mark.parametrize("value", [" ", "  \t", "\n"])
    def test_whitespace_only_is_unset(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", value)
        monkeypatch.setenv("WHATSAPP_APP_SECRET", value)

        settings = Settings(_env_file=None)

        assert settings.twilio.auth_token == ""
        assert settings.whatsapp.app_secret == ""

    def test_a_real_secret_loses_only_its_surrounding_whitespace(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", " token\n")
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret\n")

        settings = Settings(_env_file=None)

        assert settings.twilio.auth_token == "token"
        assert settings.whatsapp.app_secret == "secret"

    def test_turncall_env_left_unset_is_said_to_be_the_default(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("TURNCALL_ENV", raising=False)
        assert Settings(_env_file=None).environment_is_default

        monkeypatch.setenv("TURNCALL_ENV", "development")
        assert not Settings(_env_file=None).environment_is_default
