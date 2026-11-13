"""The carriers' μ-law is byte-identical to audioop's (#453).

Both serializers moved from stdlib `audioop` (removed in Python 3.13) to
pipecat's `g711`. This compares every 16-bit sample and every μ-law byte with
audioop's output, through each serializer, while 3.12 still has audioop.
"""

import base64
import json
import warnings

import pytest
from pipecat.frames.frames import OutputAudioRawFrame

from turncall.orchestrator.asterisk_serializer import AsteriskFrameSerializer
from turncall.orchestrator.serializer import TwilioFrameSerializer

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

pytestmark = pytest.mark.unit

# Every 16-bit sample once (a whole number of 20 ms frames: 65536 = 160 * 409.6,
# so the Asterisk serializer pads the tail; compare the samples sent).
EVERY_SAMPLE = b"".join(
    i.to_bytes(2, "little", signed=True) for i in range(-32768, 32768)
)
EVERY_ULAW = bytes(range(256))


def _out(audio: bytes) -> OutputAudioRawFrame:
    return OutputAudioRawFrame(audio=audio, sample_rate=8000, num_channels=1)


async def test_twilio_encodes_like_audioop() -> None:
    message = await TwilioFrameSerializer(stream_sid="SID").serialize(
        _out(EVERY_SAMPLE)
    )
    assert isinstance(message, str)
    sent = base64.b64decode(json.loads(message)["media"]["payload"])
    assert sent == audioop.lin2ulaw(EVERY_SAMPLE, 2)


async def test_twilio_decodes_like_audioop() -> None:
    payload = base64.b64encode(EVERY_ULAW).decode("ascii")
    frame = await TwilioFrameSerializer().deserialize(
        json.dumps({"event": "media", "media": {"payload": payload}})
    )
    assert frame is not None
    assert frame.audio == audioop.ulaw2lin(EVERY_ULAW, 2)


async def test_asterisk_encodes_like_audioop() -> None:
    sent = await AsteriskFrameSerializer().serialize(_out(EVERY_SAMPLE))
    assert isinstance(sent, bytes)
    assert sent[: len(EVERY_SAMPLE) // 2] == audioop.lin2ulaw(EVERY_SAMPLE, 2)


async def test_asterisk_decodes_like_audioop() -> None:
    frame = await AsteriskFrameSerializer().deserialize(EVERY_ULAW)
    assert frame is not None
    assert frame.audio == audioop.ulaw2lin(EVERY_ULAW, 2)
