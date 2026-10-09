"""AssemblyAI Universal-Streaming as an STT provider (#203).

What is checked here is what reaches AssemblyAI: the connection URL pipecat
builds from the settings TurnCall hands it. That URL is the whole contract —
model, keyterms and every tuning knob are query parameters on it, and a
setting that never makes it there is a setting that silently does nothing.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig, STTConfig
from turncall.domain.providers import (
    AWS_ACCOUNT,
    CREDENTIAL_ACCOUNTS,
    STT_PROVIDERS,
    accounts,
)
from turncall.orchestrator.pipeline_factory import (
    _STT_DEFAULTS,
    _create_stt_service,
    _stt_model,
)
from turncall.services.credentials import Credentials, platform_keys

pytestmark = pytest.mark.unit


def _build(**stt) -> object:
    config = AgentConfig(stt=STTConfig(provider="assemblyai", **stt))
    return _create_stt_service(config, creds(assemblyai="aai-key"), sample_rate=8000)


def _query(service) -> dict[str, list[str]]:
    return parse_qs(urlparse(service._build_ws_url()).query)


class TestTheProviderExists:
    def test_an_agent_may_name_it(self) -> None:
        schema = AgentConfigSchema.model_validate({"stt": {"provider": "assemblyai"}})
        assert schema.stt.provider == "assemblyai"

    def test_it_is_its_own_credential_account(self) -> None:
        """So `PUT /v1/credentials/assemblyai` is accepted and `/v1/providers`
        lists it, both of which read this namespace."""
        assert "assemblyai" in CREDENTIAL_ACCOUNTS
        assert accounts()["assemblyai"] == {"stt": ["assemblyai"]}

    def test_the_platform_key_is_read_from_its_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "from-env")
        assert platform_keys(Settings())["assemblyai"] == "from-env"


class TestTheKeyResolves:
    def test_project_credential_beats_the_platform_key(self) -> None:
        config = AgentConfig(stt=STTConfig(provider="assemblyai"))
        keys = Credentials(
            platform={"assemblyai": "platform"},
            project={"assemblyai": {"api_key": "project"}},
        )
        assert _create_stt_service(config, keys)._api_key == "project"

    def test_the_platform_key_is_the_fallback(self) -> None:
        config = AgentConfig(stt=STTConfig(provider="assemblyai"))
        keys = Credentials(platform={"assemblyai": "platform"})
        assert _create_stt_service(config, keys)._api_key == "platform"

    def test_no_key_anywhere_is_reported_before_the_call(self) -> None:
        """The pre-build check every transport runs: without it AssemblyAI
        refuses the socket and the caller is heard by nothing."""
        config = AgentConfig(stt=STTConfig(provider="assemblyai"))
        keys = Credentials(platform={"deepgram": "k", "openai": "k"})
        assert keys.missing_keys(config) == ["assemblyai"]

    def test_a_project_credential_satisfies_the_check(self) -> None:
        config = AgentConfig(stt=STTConfig(provider="assemblyai"))
        keys = Credentials(
            platform={"deepgram": "k", "openai": "k"},
            project={"assemblyai": {"api_key": "p"}},
        )
        assert keys.missing_keys(config) == []


class TestTheDefaultModel:
    def test_unset_is_universal_streaming_english(self) -> None:
        """Pipecat's own default is Universal-3.5 Pro, three times the price;
        the ticket's reason for the provider is the $0.15/hr model."""
        assert _stt_model("assemblyai", "") == "universal-streaming-english"

    def test_the_legacy_deepgram_sentinel_heals(self) -> None:
        assert _stt_model("assemblyai", "nova-3-general") == (
            "universal-streaming-english"
        )

    @pytest.mark.parametrize("language", ["es", "de-DE", "fr"])
    def test_another_language_gets_the_multilingual_model(self, language) -> None:
        """The English model transcribes Spanish as English words, with no
        error to say so — so the language picks the default."""
        assert _stt_model("assemblyai", "", language) == (
            "universal-streaming-multilingual"
        )

    @pytest.mark.parametrize("language", ["en", "en-US", None])
    def test_english_or_unset_keeps_the_english_model(self, language) -> None:
        assert _stt_model("assemblyai", "", language) == ("universal-streaming-english")

    def test_an_explicit_model_passes_through(self) -> None:
        assert _stt_model("assemblyai", "universal-3-5-pro", "es") == (
            "universal-3-5-pro"
        )


class TestWhatReachesAssemblyAI:
    def test_the_resolved_model_is_on_the_url(self) -> None:
        assert _query(_build())["speech_model"] == ["universal-streaming-english"]
        spanish = _build(language="es")
        assert _query(spanish)["speech_model"] == ["universal-streaming-multilingual"]

    def test_the_transport_rate_is_the_one_sent(self) -> None:
        """A phone call's 8 kHz, not pipecat's assumption — pipecat reads this
        at setup, before the socket opens."""
        assert _build()._init_sample_rate == 8000

    def test_keyterms_travel_as_keyterms_prompt(self) -> None:
        sent = _query(_build(keyterms=["Acme", "SKU-42"]))["keyterms_prompt"]
        assert json.loads(sent[0]) == ["Acme", "SKU-42"]

    def test_no_keyterms_sends_no_parameter(self) -> None:
        assert "keyterms_prompt" not in _query(_build())

    def test_more_than_a_hundred_keyterms_are_cut_to_the_first_hundred(self) -> None:
        """AssemblyAI refuses a session with more than 100 rather than using
        some, and pipecat sends what it is given — one term over is a dead STT
        stage, not a weaker one."""
        terms = [f"term{i}" for i in range(130)]
        sent = json.loads(_query(_build(keyterms=terms))["keyterms_prompt"][0])
        assert sent == terms[:100]

    def test_keyterms_beat_the_same_key_in_extra(self) -> None:
        service = _build(keyterms=["Acme"], extra={"keyterms_prompt": ["Other"]})
        assert json.loads(_query(service)["keyterms_prompt"][0]) == ["Acme"]

    def test_extra_naming_a_setting_reaches_the_url(self) -> None:
        """Pipecat builds the URL from its declared settings only and never
        reads `extra`, so a knob left there would do nothing."""
        query = _query(_build(extra={"domain": "medical-v1", "format_turns": False}))
        assert query["domain"] == ["medical-v1"]
        assert query["format_turns"] == ["false"]

    def test_extra_cannot_override_the_model(self) -> None:
        query = _query(_build(extra={"model": "HIJACKED"}))
        assert query["speech_model"] == ["universal-streaming-english"]

    def test_an_extra_key_assemblyai_has_no_setting_for_is_warned(self) -> None:
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            _build(extra={"made_up_key": 1})
        finally:
            logger.remove(sink)
        assert any("made_up_key" in line for line in seen)

    def test_pipecat_mode_turn_detection(self) -> None:
        """TurnCall's VAD and Smart Turn decide when the caller is done;
        AssemblyAI is asked for finals as fast as it can give them."""
        assert _build()._vad_force_turn_endpoint is True


class TestEveryProviderIsWiredEndToEnd:
    """Structural: the namespace a validator and `/v1/providers` read is also
    the set the factory can build, has a default for, and has a platform key
    for. A provider added to one and not the others is accepted at create and
    dies at the first call."""

    def test_every_stt_provider_has_a_default_model(self) -> None:
        assert set(_STT_DEFAULTS) == set(STT_PROVIDERS)

    @pytest.mark.parametrize("provider", STT_PROVIDERS)
    async def test_every_stt_provider_builds(self, provider, monkeypatch) -> None:
        from turncall.adapters.aiohttp_client import close_aiohttp_session

        config = AgentConfig(stt=STTConfig(provider=provider))
        keys = Credentials(platform=dict.fromkeys(STT_PROVIDERS, "k"))
        try:
            assert _create_stt_service(config, keys) is not None
        finally:
            await close_aiohttp_session()

    def test_every_key_account_has_a_platform_env_var(self) -> None:
        assert CREDENTIAL_ACCOUNTS - {AWS_ACCOUNT} <= set(platform_keys(Settings()))


def test_an_unknown_provider_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        AgentConfigSchema.model_validate({"stt": {"provider": "assembly"}})
