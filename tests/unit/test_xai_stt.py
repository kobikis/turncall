"""xAI speech-to-text as an STT provider (#394, parent #385).

What is checked here is what reaches xAI: the connection URL the service
builds — model, rate, keyterms and every tuning knob are query parameters on
it — and the messages it sends on the socket. The socket itself is the live
file's (`tests/live/test_live_stt_defaults.py`).
"""

from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, STTConfig
from turncall.domain.providers import STT_PROVIDERS
from turncall.orchestrator.pipeline_factory import _create_stt_service
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit


def _build(sample_rate: int = 8000, **stt) -> object:
    config = AgentConfig(stt=STTConfig(provider="xai", **stt))
    return _create_stt_service(config, creds(xai="xai-key"), sample_rate=sample_rate)


def _query(service) -> dict[str, list[str]]:
    service._sample_rate = service._init_sample_rate  # what setup() does
    return parse_qs(urlparse(service._build_ws_url()).query)


class TestTheProviderExists:
    def test_an_agent_may_name_it(self) -> None:
        schema = AgentConfigSchema.model_validate({"stt": {"provider": "xai"}})
        assert schema.stt.provider == "xai"

    def test_it_is_an_stt_provider(self) -> None:
        assert "xai" in STT_PROVIDERS

    def test_no_key_anywhere_is_reported_before_the_call(self) -> None:
        config = AgentConfig(stt=STTConfig(provider="xai"))
        keys = Credentials(platform={"deepgram": "k", "openai": "k"})
        assert keys.missing_keys(config) == ["xai"]


class TestWhatReachesXAI:
    def test_no_model_is_grok_voice_transcribe(self) -> None:
        assert _query(_build())["model"] == ["grok-voice-transcribe-2.0"]

    def test_the_legacy_deepgram_sentinel_heals(self) -> None:
        assert _query(_build(model="nova-3-general"))["model"] == [
            "grok-voice-transcribe-2.0"
        ]

    @pytest.mark.parametrize("rate", [8000, 16000])
    def test_the_transport_rate_is_the_one_sent(self, rate) -> None:
        """A phone call's 8 kHz or WebRTC's 16 kHz: the URL declares the rate
        of the audio that follows."""
        assert _query(_build(sample_rate=rate))["sample_rate"] == [str(rate)]

    def test_the_xai_key_is_the_one_used(self) -> None:
        config = AgentConfig(stt=STTConfig(provider="xai"))
        keys = Credentials(
            platform={"xai": "platform"}, project={"xai": {"api_key": "project"}}
        )
        assert _create_stt_service(config, keys)._api_key == "project"

    def test_the_language_is_sent(self) -> None:
        assert _query(_build())["language"] == ["en"]
        assert _query(_build(language="es"))["language"] == ["es"]

    def test_a_regional_language_is_sent_as_its_base_code(self) -> None:
        """xAI takes two-letter codes; an agent's `en-US` is English."""
        assert _query(_build(language="en-US"))["language"] == ["en"]


def _warnings(build) -> list[str]:
    from loguru import logger

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        build()
    finally:
        logger.remove(sink)
    return seen


class TestKeyterms:
    """xAI's `keyterm` is a repeatable query parameter that pipecat never
    sends. xAI refuses the whole connection over 100 terms or for one term
    over 50 characters (measured: a 400 at handshake), so either would be a
    dead STT stage rather than a weaker one."""

    def test_keyterms_travel_as_repeated_keyterm(self) -> None:
        query = _query(_build(keyterms=["Acme", "Kobi Kisos"]))
        assert query["keyterm"] == ["Acme", "Kobi Kisos"]

    def test_no_keyterms_sends_no_parameter(self) -> None:
        assert "keyterm" not in _query(_build())

    def test_a_term_over_fifty_characters_is_dropped_and_warned(self) -> None:
        long = "x" * 51
        seen = _warnings(lambda: _build(keyterms=["Acme", long]))
        assert _query(_build(keyterms=["Acme", long]))["keyterm"] == ["Acme"]
        assert any("xai_keyterms_dropped" in line for line in seen)

    def test_a_term_of_exactly_fifty_characters_is_kept(self) -> None:
        term = "y" * 50
        assert _query(_build(keyterms=[term]))["keyterm"] == [term]

    def test_more_than_a_hundred_are_cut_to_the_first_hundred(self) -> None:
        terms = [f"term{i}" for i in range(130)]
        seen = _warnings(lambda: _build(keyterms=terms))
        assert _query(_build(keyterms=terms))["keyterm"] == terms[:100]
        assert any("xai_keyterms_truncated" in line for line in seen)

    def test_keyterms_beat_the_same_key_in_extra(self) -> None:
        service = _build(keyterms=["Acme"], extra={"keyterm": ["Other"]})
        assert _query(service)["keyterm"] == ["Acme"]


class TestExtra:
    """Pipecat sends its declared settings and drops every `extra` key, so
    each knob xAI takes is put where it reaches the URL."""

    def test_a_setting_backed_key_lands_on_the_setting(self) -> None:
        service = _build(extra={"endpointing": 200, "interim_results": False})
        assert service._settings.endpointing == 200
        query = _query(service)
        assert query["endpointing"] == ["200"]
        assert query["interim_results"] == ["false"]

    def test_diarize_reaches_the_url(self) -> None:
        assert _query(_build(extra={"diarize": True}))["diarize"] == ["true"]

    def test_url_only_keys_reach_the_url(self) -> None:
        query = _query(
            _build(
                extra={
                    "format": True,
                    "filler_words": False,
                    "vad_threshold": 0.05,
                    "smart_turn": True,
                    "smart_turn_timeout": 1500,
                }
            )
        )
        assert query["format"] == ["true"]
        assert query["filler_words"] == ["false"]
        assert query["vad_threshold"] == ["0.05"]
        assert query["smart_turn"] == ["true"]
        assert query["smart_turn_timeout"] == ["1500"]

    def test_an_unknown_key_is_warned_and_not_sent(self) -> None:
        seen = _warnings(lambda: _build(extra={"made_up_key": 1}))
        assert "made_up_key" not in _query(_build(extra={"made_up_key": 1}))
        assert any(
            "xai_stt_extra_ignored" in line and "made_up_key" in line for line in seen
        )

    def test_extra_cannot_override_the_model_or_rate(self) -> None:
        query = _query(_build(extra={"model": "HIJACKED", "sample_rate": 44100}))
        assert query["model"] == ["grok-voice-transcribe-2.0"]
        assert query["sample_rate"] == ["8000"]


class _Socket:
    """An open xAI socket: records what the service sends on it."""

    def __init__(self) -> None:
        from websockets.protocol import State

        self.state = State.OPEN
        self.sent: list[object] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass


async def _run(service, frames: list, *, ready: bool = True) -> _Socket:
    """Drives the service through pipecat's own test runner, on a socket that
    is already open (the connect is xAI's, and the live file's)."""
    from unittest.mock import AsyncMock

    from pipecat.tests.utils import run_test

    socket = _Socket()
    service._connect = AsyncMock()
    service._disconnect = AsyncMock()
    service._websocket = socket
    if ready:
        service._session_ready.set()
    await run_test(service, frames_to_send=frames)
    return socket


def _finalizes(socket: _Socket) -> int:
    import json

    return sum(
        1
        for m in socket.sent
        if isinstance(m, str) and json.loads(m) == {"type": "finalize"}
    )


class TestFinalize:
    """xAI holds a final transcript until its own endpointing silence (400 ms
    by default) unless asked. Measured on 8 kHz: the final came 0.86 s after
    speech by default and 0.23 s with `finalize` sent at VAD stop — which is
    when TurnCall's turn starts waiting on it."""

    async def test_the_callers_stop_asks_for_the_final(self) -> None:
        from pipecat.frames.frames import (
            VADUserStartedSpeakingFrame,
            VADUserStoppedSpeakingFrame,
        )

        socket = await _run(
            _build(), [VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame()]
        )
        assert _finalizes(socket) == 1

    async def test_nothing_is_sent_before_the_session_is_ready(self) -> None:
        """xAI takes nothing before `transcript.created`; pipecat holds the
        audio back for the same reason."""
        from pipecat.frames.frames import VADUserStoppedSpeakingFrame

        socket = await _run(_build(), [VADUserStoppedSpeakingFrame()], ready=False)
        assert _finalizes(socket) == 0


def test_it_serves_as_a_failover_backup() -> None:
    from turncall.orchestrator.failover import FailoverSTT
    from turncall.orchestrator.pipeline_factory import _create_stt_stage
    from turncall.orchestrator.xai_stt import TurnCallXAISTT

    config = AgentConfig.model_validate({"stt": {"failover": [{"provider": "xai"}]}})
    stt = _create_stt_stage(config, creds(deepgram="dg", xai="x"), sample_rate=8000)
    assert isinstance(stt, FailoverSTT)
    assert isinstance(stt.services[1], TurnCallXAISTT)
    assert stt.label(stt.services[1]) == "xai/grok-voice-transcribe-2.0"


class TestARefusedKeyIsNamed:
    """xAI answers a wrong key's handshake with a 400, not a 401 (measured:
    `{"code":"Client specified an invalid argument","error":"Incorrect API
    key provided..."}`), which pipecat files as a bad request."""

    @staticmethod
    async def _handshake_refused(body: bytes):
        """The ErrorFrame xAI STT pushes when its connect is refused."""
        from unittest.mock import AsyncMock

        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response

        service = _build()
        service._sample_rate = 8000
        service._websocket_connect = AsyncMock(
            side_effect=InvalidStatus(Response(400, "", Headers(), body))
        )
        service.push_frame = AsyncMock()
        await service._connect_websocket()
        (frame,) = [c.args[0] for c in service.push_frame.await_args_list]
        return service, frame

    async def test_a_wrong_key_ends_as_key_rejected(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4

        from tests.unit.test_call_session import _make_call_session

        service, frame = await self._handshake_refused(
            b'{"code":"Client specified an invalid argument","error":"Incorrect '
            b"API key provided: xa***. You can obtain an API key from "
            b'https://console.x.ai."}'
        )
        call = _make_call_session(uuid4())
        seen = SimpleNamespace(
            error=frame.error,
            fatal=frame.fatal,
            processor=service,
            category=frame.category,
        )
        with patch.object(call, "_log_event", AsyncMock()) as log_event:
            await call._on_pipeline_error(seen)
        payload = log_event.await_args.args[1]
        assert (payload["provider"], payload["key_rejected"]) == ("xai", True)

    async def test_any_other_bad_request_is_not_the_key(self) -> None:
        """An unknown parameter value is the agent's mistake."""
        from pipecat.utils.errors import ErrorCategory

        _, frame = await self._handshake_refused(b'{"error":"invalid sample_rate"}')
        assert frame.category is ErrorCategory.INVALID_REQUEST


def test_the_socket_closes_without_waiting_on_xai() -> None:
    """xAI never answers the close handshake, so pipecat's default 2 s close
    timeout held every call's end that long after the goodbye (measured
    2.15-2.2 s against the live endpoint). Nothing it would still send matters
    once the call is ending."""
    assert _build()._ws_close_timeout <= 0.25
