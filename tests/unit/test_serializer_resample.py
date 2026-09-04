"""Serializer resampling continuity.

The old serializer passed None as ratecv state every frame, resetting the
filter at each boundary and producing clicks on continuous audio. This checks
that state is carried so two consecutive chunks match resampling the whole
signal at once (continuity), which the stateless version fails.
"""

import audioop
import base64
import json

import pytest
from pipecat.frames.frames import OutputAudioRawFrame

from turncall.orchestrator.serializer import TwilioFrameSerializer


def _sine_16k(n: int) -> bytes:
    # 16-bit PCM ramp/sine-ish signal; content doesn't matter, continuity does.
    import math

    return b"".join(
        int(10000 * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(
            2, "little", signed=True
        )
        for i in range(n)
    )


def _payload(msg: str) -> bytes:
    audio_mulaw = base64.b64decode(json.loads(msg)["media"]["payload"])
    return audioop.ulaw2lin(audio_mulaw, 2)  # back to PCM16 8k for comparison


@pytest.mark.asyncio
async def test_resample_state_is_continuous_across_frames() -> None:
    full = _sine_16k(640)  # 40ms at 16kHz
    half = len(full) // 2
    chunk_a, chunk_b = full[:half], full[half:]

    ser = TwilioFrameSerializer(stream_sid="SID")

    def frame(audio: bytes) -> OutputAudioRawFrame:
        return OutputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1)

    out_a = await ser.serialize(frame(chunk_a))
    out_b = await ser.serialize(frame(chunk_b))
    streamed = _payload(out_a) + _payload(out_b)

    # Reference: resample the whole signal in one shot (perfectly continuous).
    whole_8k = audioop.ratecv(full, 2, 1, 16000, 8000, None)[0]
    reference = audioop.lin2ulaw(whole_8k, 2)
    reference = audioop.ulaw2lin(reference, 2)

    # With carried state the streamed output must equal the one-shot reference.
    assert streamed == reference, "resampler state not carried across frames"


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_resample_state_is_continuous_across_frames())
    print("ok")
