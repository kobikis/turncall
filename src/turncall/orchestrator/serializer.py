"""Twilio Media Stream frame serializer for Pipecat.

Converts between Twilio's JSON-based Media Stream protocol and Pipecat frames.
Twilio sends/receives mulaw 8kHz audio as base64 in JSON messages.
Pipecat expects PCM16 (linear16) audio in InputAudioRawFrame/OutputAudioRawFrame.

Twilio → Pipecat:
  {"event": "media", "media": {"payload": "<base64 mulaw>"}} → InputAudioRawFrame(PCM16)

Pipecat → Twilio:
  OutputAudioRawFrame(PCM16) → Twilio "media" JSON with base64 mulaw payload
"""

import audioop
import base64
import json
from dataclasses import dataclass

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

TWILIO_SAMPLE_RATE = 8000
TWILIO_CHANNELS = 1


@dataclass(frozen=True)
class TwilioStreamMetadata:
    """Metadata extracted from Twilio's 'start' event."""

    stream_sid: str
    call_sid: str
    call_id: str = ""
    project_id: str = ""
    agent_id: str = ""


class TwilioFrameSerializer(FrameSerializer):
    """Serializes/deserializes between Twilio Media Stream JSON and Pipecat frames.

    Handles mulaw ↔ PCM16 conversion since Pipecat operates on PCM16
    but Twilio uses mulaw encoding.
    """

    def __init__(self, stream_sid: str = "", **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._stream_sid = stream_sid
        self._metadata: TwilioStreamMetadata | None = None
        self._logged_first_audio = False
        # ratecv filter state, carried across frames. Passing None every frame
        # (the old code) reset the filter at each ~20ms boundary, producing
        # clicks/aliasing on continuous TTS audio — the exact bug audio_resampler
        # was written to fix. ponytail: audioop carry-state, not SOXR; audioop is
        # removed in 3.13 — swap to create_stream_resampler then.
        self._resample_state: object | None = None
        self._resample_rate: int | None = None

    @property
    def stream_sid(self) -> str:
        return self._stream_sid

    @property
    def metadata(self) -> TwilioStreamMetadata | None:
        return self._metadata

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Convert a Pipecat output frame to Twilio JSON message."""
        if self.should_ignore_frame(frame):
            return None

        if not isinstance(frame, OutputAudioRawFrame):
            return None

        if not self._stream_sid:
            return None

        # PCM16 (possibly 16kHz+) → resample to 8kHz → mulaw for Twilio
        audio = frame.audio
        if frame.sample_rate != 8000:
            # Reset filter state if the input rate ever changes (it shouldn't
            # mid-call), otherwise carry it so the resampler stays continuous.
            if frame.sample_rate != self._resample_rate:
                self._resample_state = None
                self._resample_rate = frame.sample_rate
            audio, self._resample_state = audioop.ratecv(
                audio, 2, 1, frame.sample_rate, 8000, self._resample_state
            )
        mulaw_audio = audioop.lin2ulaw(audio, 2)
        payload = base64.b64encode(mulaw_audio).decode("ascii")

        message = {
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {"payload": payload},
        }
        return json.dumps(message)

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Convert a Twilio JSON message to a Pipecat input frame."""
        if isinstance(data, bytes):
            data = data.decode("utf-8")

        message = json.loads(data)
        event = message.get("event")

        if event == "media":
            frame = self._deserialize_media(message)
            if frame and not self._logged_first_audio:
                from loguru import logger

                logger.info(
                    "First audio frame: {size} bytes, rate={rate}",
                    size=len(frame.audio),
                    rate=frame.sample_rate,
                )
                self._logged_first_audio = True
            return frame
        elif event == "start":
            self._handle_start(message)
            return None
        elif event == "connected":
            return None
        elif event == "mark":
            return None
        elif event == "stop":
            return None

        return None

    def _deserialize_media(self, message: dict) -> InputAudioRawFrame | None:
        """Convert Twilio media event to InputAudioRawFrame."""
        media = message.get("media", {})
        payload = media.get("payload")
        if not payload:
            return None

        # Base64 decode → mulaw → PCM16 (stay at 8kHz, no resampling)
        mulaw_bytes = base64.b64decode(payload)
        pcm_audio = audioop.ulaw2lin(mulaw_bytes, 2)

        return InputAudioRawFrame(
            audio=pcm_audio,
            sample_rate=TWILIO_SAMPLE_RATE,
            num_channels=TWILIO_CHANNELS,
        )

    def _handle_start(self, message: dict) -> None:
        """Extract metadata from Twilio start event."""
        start_data = message.get("start", {})
        self._stream_sid = start_data.get("streamSid", "")
        custom_params = start_data.get("customParameters", {})

        self._metadata = TwilioStreamMetadata(
            stream_sid=self._stream_sid,
            call_sid=start_data.get("callSid", ""),
            call_id=custom_params.get("call_id", ""),
            project_id=custom_params.get("project_id", ""),
            agent_id=custom_params.get("agent_id", ""),
        )

    def build_clear_message(self) -> str:
        """Build a Twilio 'clear' message to flush the audio buffer."""
        return json.dumps({"event": "clear", "streamSid": self._stream_sid})

    def build_mark_message(self, name: str) -> str:
        """Build a Twilio 'mark' message for playback tracking."""
        return json.dumps(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": name},
            }
        )
