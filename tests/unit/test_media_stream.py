"""Tests for TwilioFrameSerializer (Pipecat-based media stream)."""

import audioop
import base64
import json

import pytest

from turncall.orchestrator.serializer import (
    TWILIO_CHANNELS,
    TWILIO_SAMPLE_RATE,
    TwilioFrameSerializer,
)


def _make_mulaw_payload(pcm_bytes: bytes) -> str:
    """Convert PCM16 bytes to base64-encoded mulaw for Twilio messages."""
    mulaw = audioop.lin2ulaw(pcm_bytes, 2)
    return base64.b64encode(mulaw).decode("ascii")


@pytest.mark.unit
class TestSerializerDeserialization:
    @pytest.mark.asyncio
    async def test_deserialize_media_event(self) -> None:
        serializer = TwilioFrameSerializer(stream_sid="MZ_test")
        pcm_audio = b"\x00\x01" * 160
        payload = _make_mulaw_payload(pcm_audio)

        twilio_msg = json.dumps(
            {
                "event": "media",
                "media": {"payload": payload, "track": "inbound"},
            }
        )

        frame = await serializer.deserialize(twilio_msg)
        assert frame is not None
        assert frame.sample_rate == TWILIO_SAMPLE_RATE
        assert frame.num_channels == TWILIO_CHANNELS
        assert len(frame.audio) > 0

    @pytest.mark.asyncio
    async def test_deserialize_start_event_extracts_metadata(self) -> None:
        serializer = TwilioFrameSerializer()
        twilio_msg = json.dumps(
            {
                "event": "start",
                "start": {
                    "streamSid": "MZ_stream_123",
                    "callSid": "CA_call_456",
                    "customParameters": {
                        "call_id": "uuid-call",
                        "project_id": "uuid-project",
                        "agent_id": "uuid-assistant",
                    },
                },
            }
        )

        frame = await serializer.deserialize(twilio_msg)
        assert frame is None

        assert serializer.stream_sid == "MZ_stream_123"
        assert serializer.metadata is not None
        assert serializer.metadata.call_sid == "CA_call_456"
        assert serializer.metadata.call_id == "uuid-call"

    @pytest.mark.asyncio
    async def test_deserialize_connected_returns_none(self) -> None:
        serializer = TwilioFrameSerializer()
        frame = await serializer.deserialize(json.dumps({"event": "connected"}))
        assert frame is None

    @pytest.mark.asyncio
    async def test_deserialize_stop_returns_none(self) -> None:
        serializer = TwilioFrameSerializer()
        frame = await serializer.deserialize(json.dumps({"event": "stop"}))
        assert frame is None

    @pytest.mark.asyncio
    async def test_deserialize_mark_returns_none(self) -> None:
        serializer = TwilioFrameSerializer()
        frame = await serializer.deserialize(
            json.dumps({"event": "mark", "mark": {"name": "test"}})
        )
        assert frame is None


@pytest.mark.unit
class TestSerializerSerialization:
    @pytest.mark.asyncio
    async def test_serialize_output_audio_frame(self) -> None:
        from pipecat.frames.frames import OutputAudioRawFrame

        serializer = TwilioFrameSerializer(stream_sid="MZ_test")
        pcm_audio = b"\x00\x80" * 160
        frame = OutputAudioRawFrame(audio=pcm_audio, sample_rate=8000, num_channels=1)

        result = await serializer.serialize(frame)
        assert result is not None

        msg = json.loads(result)
        assert msg["event"] == "media"
        assert msg["streamSid"] == "MZ_test"
        assert "payload" in msg["media"]

        # Verify round-trip
        decoded = base64.b64decode(msg["media"]["payload"])
        reconstructed = audioop.ulaw2lin(decoded, 2)
        assert len(reconstructed) == len(pcm_audio)

    @pytest.mark.asyncio
    async def test_serialize_without_stream_sid_returns_none(self) -> None:
        from pipecat.frames.frames import OutputAudioRawFrame

        serializer = TwilioFrameSerializer(stream_sid="")
        frame = OutputAudioRawFrame(
            audio=b"\x00" * 320, sample_rate=8000, num_channels=1
        )
        result = await serializer.serialize(frame)
        assert result is None

    @pytest.mark.asyncio
    async def test_serialize_non_audio_frame_returns_none(self) -> None:
        from pipecat.frames.frames import TextFrame

        serializer = TwilioFrameSerializer(stream_sid="MZ_test")
        frame = TextFrame(text="hello")
        result = await serializer.serialize(frame)
        assert result is None


@pytest.mark.unit
class TestSerializerHelpers:
    def test_build_clear_message(self) -> None:
        serializer = TwilioFrameSerializer(stream_sid="MZ_test")
        msg = json.loads(serializer.build_clear_message())
        assert msg["event"] == "clear"
        assert msg["streamSid"] == "MZ_test"

    def test_build_mark_message(self) -> None:
        serializer = TwilioFrameSerializer(stream_sid="MZ_test")
        msg = json.loads(serializer.build_mark_message("utterance-1-end"))
        assert msg["event"] == "mark"
        assert msg["mark"]["name"] == "utterance-1-end"

    def test_stream_sid_updated_on_start(self) -> None:
        serializer = TwilioFrameSerializer()
        assert serializer.stream_sid == ""
        serializer._handle_start(
            {
                "start": {
                    "streamSid": "MZ_new",
                    "callSid": "CA_test",
                    "customParameters": {},
                }
            }
        )
        assert serializer.stream_sid == "MZ_new"
