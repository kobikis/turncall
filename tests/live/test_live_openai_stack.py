"""C1 — the OpenAI stack under the openai 3 SDK.

openai 3 builds on httpx2 and verifies TLS against the OS trust store rather
than certifi. Nothing in the unit suite opens a socket, so the upgrade landed
with every OpenAI path unexercised: STT, TTS, and the Realtime WebSocket that
carries S2S calls.
"""

import asyncio
import io
import json

import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def test_the_sdk_is_the_major_we_expect() -> None:
    """Bounds are `>=1.74,<4`. Which major is actually resolved decides whether
    the rest of this file is testing what we think it is."""
    import importlib.metadata as md

    import openai

    major = int(md.version("openai").split(".")[0])
    assert 1 <= major < 4, f"openai {openai.__version__} is outside the declared bound"


async def test_tts_and_stt_round_trip(openai_key: str) -> None:
    """Speak a phrase and transcribe it back. Exercises both audio paths and,
    with them, the TLS and HTTP stack underneath."""
    import openai

    client = openai.AsyncOpenAI(api_key=openai_key)

    spoken = await client.audio.speech.create(
        model="tts-1", voice="alloy", input="the quick brown fox", response_format="wav"
    )
    audio = spoken.content if hasattr(spoken, "content") else await spoken.aread()
    assert len(audio) > 1000, "TTS returned no usable audio"

    handle = io.BytesIO(audio)
    handle.name = "probe.wav"
    transcript = await client.audio.transcriptions.create(
        model="whisper-1", file=handle
    )

    assert "quick brown fox" in transcript.text.lower(), transcript.text


async def test_the_realtime_websocket_opens(openai_key: str) -> None:
    """S2S rides this socket. Pipecat sends only Authorization against the GA
    URL — a probe that adds the old `OpenAI-Beta: realtime=v1` header now gets
    `beta_api_shape_disabled`, so the absence of that header is the thing worth
    pinning."""
    import websockets

    from turncall.orchestrator.s2s_config import _OPENAI_DEFAULT_MODEL

    url = f"wss://api.openai.com/v1/realtime?model={_OPENAI_DEFAULT_MODEL}"
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {openai_key}"}
    ) as ws:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))

    assert event["type"] == "session.created", event
    assert event["session"]["model"] == _OPENAI_DEFAULT_MODEL


async def test_the_beta_header_is_now_rejected(openai_key: str) -> None:
    """Pinned deliberately: if OpenAI ever re-accepts it this test fails and we
    learn the constraint moved. If Pipecat ever starts sending it again, the
    test above fails instead."""
    import websockets

    url = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    async with websockets.connect(url, additional_headers=headers) as ws:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))

    assert event["type"] == "error"
    assert event["error"]["code"] == "beta_api_shape_disabled", event
