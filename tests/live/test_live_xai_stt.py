"""xAI speech-to-text against the real API (#394).

What the unit suite takes on trust, because each is xAI's rule: that the
default model is served at a phone call's 8 kHz, that the URL TurnCall adds
to (`keyterm`) is accepted, that `finalize` brings the final transcript, and
that a wrong key's handshake reads as one.

Needs XAI_API_KEY. The caller's line is synthesized by xAI's own TTS, on the
same key.
"""

import httpx
import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.utils.errors import ErrorCategory

from turncall.domain.models import AgentConfig, STTConfig
from turncall.orchestrator.pipeline_factory import _create_stt_service
from turncall.services.credentials import Credentials

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_RATE = 8000
_CHUNK = _RATE // 10 * 2  # 100 ms of 16-bit mono


def _speech(key: str, text: str) -> bytes:
    response = httpx.post(
        "https://api.x.ai/v1/tts",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "text": text,
            "voice_id": "rex",
            "language": "en",
            "output_format": {"codec": "pcm", "sample_rate": _RATE},
        },
        timeout=30,
    )
    response.raise_for_status()
    audio = response.content
    return audio[: len(audio) // 2 * 2]


def _caller(audio: bytes) -> list:
    """The caller speaks in real time, then VAD hears them stop."""
    frames: list = [SleepFrame(1.5), VADUserStartedSpeakingFrame()]
    for i in range(0, len(audio), _CHUNK):
        frames += [InputAudioRawFrame(audio[i : i + _CHUNK], _RATE, 1), SleepFrame(0.1)]
    silence = bytes(_CHUNK)
    frames.append(VADUserStoppedSpeakingFrame())
    for _ in range(15):
        frames += [InputAudioRawFrame(silence, _RATE, 1), SleepFrame(0.1)]
    return frames


def _stt(key: str, **stt):
    config = AgentConfig(stt=STTConfig(provider="xai", **stt))
    return _create_stt_service(
        config, Credentials(platform={"xai": key}), sample_rate=_RATE
    )


async def test_a_phone_line_clip_transcribes(xai_key: str) -> None:
    audio = _speech(xai_key, "I would like to book an appointment for Tuesday morning.")
    service = _stt(xai_key, keyterms=["Tuesday", "Kobi Kisos"])
    down, up = await run_test(service, frames_to_send=_caller(audio))

    errors = [f.error for f in up if isinstance(f, ErrorFrame)]
    finals = [f.text for f in down if isinstance(f, TranscriptionFrame)]
    assert not errors, errors
    assert any("tuesday" in text.lower() for text in finals), finals


async def test_a_wrong_key_is_refused_as_a_key(xai_key: str) -> None:
    """`xai_key` only gates the file: the key used here is a wrong one."""
    service = _stt("xai-not-a-real-key")
    _, up = await run_test(service, frames_to_send=[SleepFrame(2.0)])

    refused = [f for f in up if isinstance(f, ErrorFrame)]
    assert refused, "no error for a wrong key"
    assert refused[0].category is ErrorCategory.AUTHENTICATION, refused[0].error
