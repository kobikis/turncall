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
