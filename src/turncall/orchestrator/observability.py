"""Pipecat frame processor for observability.

Taps into the frame flow to log transcripts, tool calls,
and latency metrics to the database. Dispatches events to
webhook subscribers.

TranscriptTapProcessor is placed early in the pipeline (after STT)
to capture transcription frames before they are consumed by the
context aggregator. ObservabilityProcessor is placed at the end
of the pipeline for non-transcript events.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from turncall.domain.enums import CallEventType

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

# Fire-and-forget logging tasks. Kept in a set so they aren't garbage-collected
# mid-flight (asyncio holds only a weak ref). ponytail: a worst-case lost final
# transcript on call teardown is acceptable; not worth a drain step.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:  # type: ignore[no-untyped-def]
    """Run a logging coroutine off the live frame path so a slow DB write or
    webhook never stalls audio."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


class TranscriptTapProcessor(FrameProcessor):
    """Placed after STT to capture TranscriptionFrames before
    they are consumed by the context aggregator.

    Logs customer speech to the database as transcript.final events.
    """

    def __init__(self, call_context: CallContext, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._call_context = call_context

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.text and frame.text.strip():
            _spawn(self._log_transcript(frame.text, frame.user_id or "customer"))

        await self.push_frame(frame, direction)

    async def _log_transcript(self, text: str, role: str) -> None:

        payload = {
            "text": text,
            "role": role,
        }

        try:
            async with self._call_context.session_factory() as session:
                from turncall.storage.repositories import call_repo

                seq = await call_repo.get_next_sequence_number(
                    session, self._call_context.call_id
                )
                await call_repo.create_call_event(
                    session,
                    call_id=self._call_context.call_id,
                    event_type=CallEventType.TRANSCRIPT_FINAL,
                    payload=payload,
                    sequence_number=seq,
                )
                await session.commit()

                from turncall.events.dispatcher import dispatch_event

                await dispatch_event(
                    session,
                    project_id=self._call_context.project_id,
                    event_type=CallEventType.TRANSCRIPT_FINAL,
                    payload=payload,
                    call_id=self._call_context.call_id,
                )
        except Exception:
            logger.exception(
                "transcript_tap_error",
                call_id=str(self._call_context.call_id),
            )


class AssistantTranscriptTapProcessor(FrameProcessor):
    """Placed after LLM to capture assistant responses.

    Accumulates TextFrame tokens and flushes the complete utterance
    when LLMFullResponseEndFrame arrives.
    """

    def __init__(
        self,
        call_context: CallContext,
        llm_service: object | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._call_context = call_context
        # llm_service exposes get_full_model_name() (OpenAI-derived services) — used to
        # record which model actually answered, since OpenRouter fallback can switch it.
        self._llm_service = llm_service
        self._buffer: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame) and frame.text:
            # Directly-spoken text (first_message, voicemail/transfer messages)
            # never passes through the LLM — log it as an utterance of its own.
            _spawn(self._log_transcript(frame.text))
        elif (
            isinstance(frame, TextFrame)
            and not isinstance(frame, (TranscriptionFrame, TTSTextFrame))
            and frame.text
        ):
            # Both subclass TextFrame. Exclude TranscriptionFrame (user speech, not
            # assistant) and TTSTextFrame: S2S services push each assistant token as
            # BOTH an LLMTextFrame and a TTSTextFrame, which doubled every word.
            # Cascade's tap sits before the TTS, so it only ever sees LLMTextFrame.
            self._buffer.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._buffer:
            full_text = "".join(self._buffer).strip()
            self._buffer.clear()
            if full_text:
                _spawn(self._log_transcript(full_text))

        await self.push_frame(frame, direction)

    def _resolved_model(self) -> str | None:
        getter = getattr(self._llm_service, "get_full_model_name", None)
        if getter is None:
            return None
        try:
            return getter() or None
        except Exception:
            return None

    async def _log_transcript(self, text: str) -> None:
        payload = {
            "text": text,
            "role": "assistant",
        }
        model = self._resolved_model()
        if model:
            payload["model"] = model

        try:
            async with self._call_context.session_factory() as session:
                from turncall.storage.repositories import call_repo

                seq = await call_repo.get_next_sequence_number(
                    session, self._call_context.call_id
                )
                await call_repo.create_call_event(
                    session,
                    call_id=self._call_context.call_id,
                    event_type=CallEventType.TRANSCRIPT_FINAL,
                    payload=payload,
                    sequence_number=seq,
                )
                await session.commit()

                from turncall.events.dispatcher import dispatch_event

                await dispatch_event(
                    session,
                    project_id=self._call_context.project_id,
                    event_type=CallEventType.TRANSCRIPT_FINAL,
                    payload=payload,
                    call_id=self._call_context.call_id,
                )
        except Exception:
            logger.exception(
                "assistant_tap_error",
                call_id=str(self._call_context.call_id),
            )


class ObservabilityProcessor(FrameProcessor):
    """Placed at the end of the pipeline for non-transcript events."""

    def __init__(self, call_context: CallContext, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._call_context = call_context

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
