"""Asterisk chan_websocket media <-> Pipecat frames (#209).

The external-media channel TurnCall asks ARI for is a chan_websocket channel
(Asterisk 20.18 / 22.8 / 23.2 or later). On its WebSocket:

- **binary** messages are raw media in the channel's format — ulaw at 8 kHz
  here, no RTP header, 160 bytes per 20 ms both ways;
- **text** messages are JSON control events from Asterisk (`MEDIA_START`,
  `DTMF_END`, `MEDIA_XOFF`, ...) and plain-text commands to it (`FLUSH_MEDIA`).

Asterisk re-times what it is sent and queues up to ~20 s of it, so on a
barge-in the agent's queued speech must be discarded there: an interruption
sends `FLUSH_MEDIA`, Twilio's `clear`. Media should arrive in whole frames
(odd sizes risk dropped audio), so every payload is padded to one. Pipecat
ships no serializer for this protocol (1.12), hence this one.
"""

import audioop
import json

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

ASTERISK_SAMPLE_RATE = 8000
# One 20 ms ulaw frame: chan_websocket's optimal_frame_size for ulaw.
FRAME_BYTES = 160
ULAW_SILENCE = b"\xff"


class AsteriskFrameSerializer(FrameSerializer):
    """ulaw binary frames in and out; FLUSH_MEDIA on an interruption."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, InterruptionFrame):
            return "FLUSH_MEDIA"
        if not isinstance(frame, OutputAudioRawFrame) or self.should_ignore_frame(
            frame
        ):
            return None
        # The transport runs at the carrier's 8 kHz, so no resampling here.
        ulaw = audioop.lin2ulaw(frame.audio, 2)
        short = -len(ulaw) % FRAME_BYTES
        return ulaw + ULAW_SILENCE * short

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            return InputAudioRawFrame(
                audio=audioop.ulaw2lin(data, 2),
                sample_rate=ASTERISK_SAMPLE_RATE,
                num_channels=1,
            )
        try:
            event = json.loads(data)
        except ValueError:
            return None  # a plain-text control message: none matters here
        if event.get("event") == "DTMF_END":
            try:
                return InputDTMFFrame(KeypadEntry(str(event.get("digit"))))
            except ValueError:
                return None
        return None
