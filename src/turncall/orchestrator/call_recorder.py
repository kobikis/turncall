"""App-side call recording.

Captures merged user+bot audio via Pipecat's AudioBufferProcessor across all
transports (Twilio, WebRTC, WhatsApp), writes a WAV to the configured object
storage on call end, and records the URL + status on the call.

Self-contained: auto-starts on StartFrame and auto-flushes on EndFrame/
CancelFrame (the base processor calls stop_recording on those, which fires
on_audio_data) — so call_session needs no start/stop wiring.
"""

from __future__ import annotations

import io
import wave
from typing import TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import Frame, StartFrame
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection

from turncall.adapters.storage import create_storage_adapter
from turncall.config import get_settings
from turncall.domain.enums import CallEventType, RecordingStatus

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext


def attach_recorder(
    transport: object, call_context: CallContext, *, sample_rate: int
) -> CallRecorder:
    """Create a recorder and flush it when the client disconnects.

    The recorder flushes on EndFrame/CancelFrame in process_frame, but the
    common Twilio path — the client hanging up — only closes the WebSocket and
    fires on_client_disconnected; no end frame propagates through the pipeline.
    Without this hook the recording would never be written (status stuck at
    in_progress). stop_recording is idempotent, so a later end frame is a no-op.
    """
    recorder = CallRecorder(call_context, sample_rate=sample_rate)

    @transport.event_handler("on_client_disconnected")  # type: ignore[attr-defined]
    async def _flush_on_disconnect(*_args: object) -> None:
        logger.info(
            "recording_flush_on_disconnect", call_id=str(call_context.call_id)
        )
        await recorder.stop_recording()

    return recorder


def _pcm16_to_wav(pcm: bytes, sample_rate: int, num_channels: int) -> bytes:
    """Wrap raw PCM16 little-endian samples in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(2)  # PCM16 = 2 bytes/sample
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class CallRecorder(AudioBufferProcessor):
    """Records the call to object storage. Mono mix of user + bot audio."""

    def __init__(self, call_context: CallContext, *, sample_rate: int) -> None:
        super().__init__(sample_rate=sample_rate, num_channels=1)
        self._call_context = call_context
        # Register the flush handler (fired on stop_recording / EndFrame).
        self.event_handler("on_audio_data")(self._on_audio_data)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Begin capturing once the pipeline starts. _recording is reset by the
        # base class; guard so we only start (and stamp status) once.
        if isinstance(frame, StartFrame) and not self._recording:
            await self.start_recording()
            await self._set_status(RecordingStatus.IN_PROGRESS)

    async def _on_audio_data(
        self,
        _buffer: AudioBufferProcessor,
        audio: bytes,
        sample_rate: int,
        num_channels: int,
    ) -> None:
        if not audio:
            await self._set_status(RecordingStatus.FAILED)
            return
        try:
            wav = _pcm16_to_wav(audio, sample_rate, num_channels)
            storage_settings = get_settings().storage
            storage = create_storage_adapter(
                backend=storage_settings.backend,
                local_path=storage_settings.local_path,
                s3_bucket=storage_settings.s3_bucket,
                aws_region=storage_settings.aws_region,
            )
            key = f"recordings/{self._call_context.call_id}.wav"
            url = await storage.upload(key, wav, content_type="audio/wav")
            await self._persist(url)
        except Exception:
            logger.exception(
                "call_recording_store_error",
                call_id=str(self._call_context.call_id),
            )
            await self._set_status(RecordingStatus.FAILED)

    async def _persist(self, url: str) -> None:
        async with self._call_context.session_factory() as session:
            from turncall.events.emit import emit_call_event
            from turncall.storage.repositories import call_repo

            await call_repo.update_call_recording_url(
                session, self._call_context.call_id, url
            )
            await call_repo.update_call_recording_status(
                session, self._call_context.call_id, RecordingStatus.COMPLETED.value
            )
            await emit_call_event(
                session,
                call_id=self._call_context.call_id,
                project_id=self._call_context.project_id,
                event_type=CallEventType.RECORDING_READY,
                payload={"recording_url": url},
            )
            await session.commit()
        logger.info(
            "call_recording_stored",
            call_id=str(self._call_context.call_id),
            url=url,
        )

    async def _set_status(self, status: RecordingStatus) -> None:
        try:
            async with self._call_context.session_factory() as session:
                from turncall.storage.repositories import call_repo

                await call_repo.update_call_recording_status(
                    session, self._call_context.call_id, status.value
                )
                await session.commit()
        except Exception:
            logger.exception(
                "call_recording_status_error",
                call_id=str(self._call_context.call_id),
            )
