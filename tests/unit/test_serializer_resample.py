"""The Twilio serializer's resampling of the agent's audio to 8 kHz (#453).

The serializer carries one stream resampler across frames: resetting the filter
at each ~20 ms boundary clicks on continuous audio. On an interruption it is
reset, so the tail of the reply the caller cut off is not sent at the start of
the next one (what pipecat 1.16's own telephony serializers do).
"""

import base64
import json
import math
from pathlib import Path

import pytest
from pipecat.audio import g711
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame

from turncall.orchestrator.serializer import TwilioFrameSerializer

pytestmark = pytest.mark.unit


def _sine_16k(n: int, hz: int = 440) -> bytes:
    return b"".join(
        int(10000 * math.sin(2 * math.pi * hz * i / 16000)).to_bytes(
            2, "little", signed=True
        )
        for i in range(n)
    )


def _frame(audio: bytes) -> OutputAudioRawFrame:
    return OutputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1)


def _pcm(message: str | bytes | None) -> bytes:
    assert isinstance(message, str)
    return g711.ulaw_to_pcm(base64.b64decode(json.loads(message)["media"]["payload"]))


async def _sent(serializer: TwilioFrameSerializer, *chunks: bytes) -> bytes:
    return b"".join([_pcm(await serializer.serialize(_frame(c))) for c in chunks])


async def test_resampling_is_continuous_across_frames() -> None:
    full = _sine_16k(640)  # 40 ms at 16 kHz
    half = len(full) // 2

    streamed = await _sent(
        TwilioFrameSerializer(stream_sid="SID"), full[:half], full[half:]
    )
    whole = await _sent(TwilioFrameSerializer(stream_sid="SID"), full)

    assert streamed == whole, "resampler state not carried across frames"


async def test_each_frame_keeps_its_pace() -> None:
    """20 ms in is 20 ms out: the output transport already paced the frames."""
    serializer = TwilioFrameSerializer(stream_sid="SID")
    for _ in range(3):
        assert len(await _sent(serializer, _sine_16k(320))) == 320  # 160 samples


async def test_an_interruption_drops_the_cut_off_replys_tail() -> None:
    first, second = _sine_16k(640, hz=440), _sine_16k(640, hz=1000)
    serializer = TwilioFrameSerializer(stream_sid="SID")
    await _sent(serializer, first)

    await serializer.serialize(InterruptionFrame())
    after_barge_in = await _sent(serializer, second)

    assert after_barge_in == await _sent(
        TwilioFrameSerializer(stream_sid="SID"), second
    )


def test_no_audioop_under_src() -> None:
    """audioop is gone in Python 3.13; pipecat ships what replaces it."""
    src = Path(__file__).resolve().parents[2] / "src"
    users = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if "import audioop" in p.read_text()
    ]
    assert users == []


def test_a_callers_params_keep_the_wire_rate() -> None:
    from pipecat.serializers.base_serializer import FrameSerializer

    serializer = TwilioFrameSerializer(
        stream_sid="MZ", params=FrameSerializer.InputParams(ignore_rtvi_messages=False)
    )

    assert serializer.audio_out_sample_rate == 8000
    assert serializer._params.ignore_rtvi_messages is False


def test_pipecats_private_resampling_surface_is_still_there() -> None:
    """The serializer resamples and resets through pipecat's private
    `FrameSerializer` methods; a bump that renames them should fail here,
    naming them, rather than at the first call's audio."""
    from pipecat.serializers.base_serializer import FrameSerializer

    for name in ("_resample_audio_out", "_reset_audio_out"):
        assert callable(getattr(FrameSerializer, name, None)), (
            f"pipecat's FrameSerializer.{name} is gone: TwilioFrameSerializer calls it"
        )
