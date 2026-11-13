"""Eleven Realtime as the `elevenlabs` S2S provider (#465).

What is checked here is what pipecat is handed and what the session update
sends. The socket itself is the live file's
(`tests/live/test_live_elevenlabs_realtime.py`).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, S2SConfig
from turncall.domain.providers import S2S_PROVIDERS, accounts
from turncall.orchestrator.s2s_config import create_s2s_service
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit


def _agent(max_call_duration_seconds: int = 3600, **s2s) -> AgentConfig:
    return AgentConfig(
        name="eleven",
        system_prompt="You are a receptionist.",
        pipeline_mode="s2s",
        max_call_duration_seconds=max_call_duration_seconds,
        s2s=S2SConfig(provider="elevenlabs", **s2s),
    )


def _service(config: AgentConfig | None = None, keys: Credentials | None = None):
    return create_s2s_service(config or _agent(), keys or creds(elevenlabs="xi-key"))


def _schema(s2s: dict, **agent) -> AgentConfigSchema:
    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": "elevenlabs", **s2s}, **agent}
    )


async def _session_update(service) -> dict:
    """The `session.update` the service puts on the socket."""
    from pipecat.services.openai.realtime import events

    service._ws_send = AsyncMock()
    session = service._settings.session_properties
    await service.send_client_event(events.SessionUpdateEvent(session=session))
    (message,) = [c.args[0] for c in service._ws_send.await_args_list]
    assert message["type"] == "session.update"
    return message["session"]


class TestTheProviderExists:
    def test_an_agent_may_name_it(self) -> None:
        assert _schema({}).s2s.provider == "elevenlabs"

    def test_it_is_an_s2s_provider_on_the_elevenlabs_account(self) -> None:
        assert "elevenlabs" in S2S_PROVIDERS
        assert accounts()["elevenlabs"]["s2s"] == ["elevenlabs"]


class TestTheService:
    def test_pipecats_realtime_service_at_elevenlabs(self) -> None:
        from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

        service = _service()

        assert isinstance(service, OpenAIRealtimeLLMService)
        assert service.base_url == (
            "wss://api.elevenlabs.io/v1/convai/realtime?model=eleven_realtime_v1_mini"
        )

    async def test_the_key_goes_as_xi_api_key(self) -> None:
        assert await _service()._auth_headers() == {"xi-api-key": "xi-key"}

    def test_max_is_passed_through(self) -> None:
        service = _service(_agent(model="eleven_realtime_v1_max"))
        assert service.base_url.endswith("?model=eleven_realtime_v1_max")

    async def test_defaults_are_elevenlabs_own(self) -> None:
        session = await _session_update(_service())
        assert session["model"] == "eleven_realtime_v1_mini"
        assert session["audio"]["output"]["voice"] == "brian"

    async def test_an_explicit_voice_is_passed_through(self) -> None:
        session = await _session_update(_service(_agent(voice="JBFqnCBsd6RMkjVDRZzb")))
        assert session["audio"]["output"]["voice"] == "JBFqnCBsd6RMkjVDRZzb"

    async def test_pcm_24k_both_ways(self) -> None:
        session = await _session_update(_service())
        pcm = {"type": "audio/pcm", "rate": 24000}
        assert session["audio"]["input"]["format"] == pcm
        assert session["audio"]["output"]["format"] == pcm

    async def test_the_prompt_is_the_instructions(self) -> None:
        session = await _session_update(_service())
        assert session["instructions"] == "You are a receptionist."


class TestTheSession:
    async def test_language_eagerness_temperature_and_tokens(self) -> None:
        config = _agent(
            temperature=0.4,
            max_tokens=300,
            extra={"language": "es", "eagerness": "low"},
        )
        session = await _session_update(_service(config))

        assert session["audio"]["input"]["transcription"] == {"language": "es"}
        assert session["audio"]["input"]["turn_detection"] == {
            "type": "semantic_vad",
            "eagerness": "low",
        }
        assert session["temperature"] == 0.4
        assert session["max_response_output_tokens"] == 300

    async def test_nothing_set_sends_no_transcription_model(self) -> None:
        """Input is always transcribed; pipecat's default Whisper model must
        not leak into ElevenLabs' session."""
        session = await _session_update(_service())
        assert "transcription" not in session["audio"]["input"]
        assert "temperature" not in session

    async def test_pipecat_vad_turns_server_detection_off(self) -> None:
        session = await _session_update(_service(_agent(turn_detection="pipecat_vad")))
        assert session["audio"]["input"]["turn_detection"] is None

    @pytest.mark.parametrize(("max_call", "sent"), [(3600, 3630), (7200, 7200)])
    async def test_the_session_outlasts_the_call(self, max_call, sent) -> None:
        """ElevenLabs closes a session at 600 s unless told otherwise."""
        session = await _session_update(_service(_agent(max_call)))
        assert session["max_duration_seconds"] == sent


_REFUSED = [
    ({"base_url": "wss://gateway.example.com/v1/realtime"}, r"s2s\.base_url"),
    ({"extra": {"language": 3}}, r"s2s\.extra\.language"),
    ({"extra": {"eagerness": "auto"}}, r"s2s\.extra\.eagerness"),
]


class TestRefusals:
    @pytest.mark.parametrize(("s2s", "match"), _REFUSED)
    def test_a_stored_agent_is_a_422(self, s2s, match) -> None:
        with pytest.raises(ValidationError, match=match):
            _schema(s2s)

    @pytest.mark.parametrize(("s2s", "match"), _REFUSED)
    def test_an_inline_agent_fails_the_build(self, s2s, match) -> None:
        with pytest.raises(ValueError, match=match):
            _service(_agent(**s2s))

    def test_a_call_longer_than_a_session_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="7200"):
            _schema({}, max_call_duration_seconds=7201)
        with pytest.raises(ValueError, match="7200"):
            _service(_agent(7201))

    def test_temperature_is_allowed(self) -> None:
        assert _schema({"temperature": 0.5}).s2s.temperature == 0.5

    def test_gateway_mode_to_elevenlabs_is_refused(self) -> None:
        """It would send the platform's OpenAI key to ElevenLabs."""
        gateway = {
            "provider": "openai",
            "base_url": "wss://api.elevenlabs.io/v1/convai/realtime",
        }
        with pytest.raises(ValidationError, match="'elevenlabs'"):
            AgentConfigSchema.model_validate({"pipeline_mode": "s2s", "s2s": gateway})
        config = _agent().model_copy(update={"s2s": S2SConfig(**gateway)})
        with pytest.raises(ValueError, match="'elevenlabs'"):
            create_s2s_service(config, creds("sk-openai"))

    def test_an_unknown_extra_key_is_dropped(self) -> None:
        from loguru import logger

        said: list[str] = []
        sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
        try:
            _service(_agent(extra={"speed": 1.2}))
        finally:
            logger.remove(sink)
        assert any("speed" in m for m in said), said


class TestTheKey:
    def test_the_elevenlabs_account_is_used(self) -> None:
        keys = Credentials(
            platform={"openai": "sk-openai", "elevenlabs": "platform"},
            project={"elevenlabs": {"api_key": "project"}},
        )
        assert _service(keys=keys).api_key == "project"

    def test_no_key_is_reported_before_the_call(self) -> None:
        keys = Credentials(platform={"openai": "sk-openai"})
        assert keys.missing_keys(_agent()) == ["elevenlabs"]

    def test_the_service_reports_as_elevenlabs(self) -> None:
        from turncall.orchestrator.call_session import provider_of

        assert provider_of(_service()) == "elevenlabs"

    @pytest.mark.parametrize(("code", "is_key"), [(4001, True), (1011, False)])
    async def test_a_closed_session_fails_the_service(self, code, is_key) -> None:
        """ElevenLabs accepts the socket with a wrong key, then closes it with
        4001 (measured); pipecat's receive loop would only log the close."""
        from pipecat.utils.errors import ErrorCategory
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close

        class Closing:
            def __aiter__(self):
                return self

            async def __anext__(self):
                close = Close(code, "Authentication required")
                raise ConnectionClosedError(close, close, rcvd_then_sent=True)

        service = _service()
        service._websocket = Closing()
        service.push_frame = AsyncMock()
        await service._receive_task_handler()

        (frame,) = [c.args[0] for c in service.push_frame.await_args_list]
        assert (frame.category is ErrorCategory.AUTHENTICATION) is is_key
        assert not service.is_usable


class TestTheS2SRulesHold:
    def test_the_idle_check_in_reaches_the_service(self) -> None:
        from turncall.orchestrator.realtime_appends import around_the_user_aggregator

        before, after = around_the_user_aggregator("elevenlabs")
        assert before and after

    def test_failover_is_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"llm\.failover"):
            _schema({}, llm={"failover": [{"provider": "anthropic"}]})

    def test_keyterms_are_warned_as_ignored(self) -> None:
        from turncall.orchestrator.config_warnings import keyterms_on_s2s_warning

        config = _agent().model_copy(update={"stt": MagicMock(keyterms=["Nonna"])})
        assert keyterms_on_s2s_warning(config) is not None
