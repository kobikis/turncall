"""The assistant transcript tap must ignore user TranscriptionFrames.

TranscriptionFrame subclasses TextFrame, and in the S2S pipeline both the user
transcription and the assistant text flow through the same tap — without the
guard, user speech would be logged as assistant.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pipecat.frames.frames import (
    AggregationType,
    LLMFullResponseEndFrame,
    TextFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from turncall.orchestrator.observability import AssistantTranscriptTapProcessor


async def _drain() -> None:
    # let _spawn()'d logging tasks run to completion
    for _ in range(3):
        await asyncio.sleep(0)


async def test_assistant_tap_excludes_user_transcription() -> None:
    tap = AssistantTranscriptTapProcessor(call_context=MagicMock())
    logged: list[str] = []
    tap._log_transcript = AsyncMock(side_effect=lambda text: logged.append(text))
    tap.push_frame = AsyncMock()

    async def feed(frame: object) -> None:
        await tap.process_frame(frame, FrameDirection.DOWNSTREAM)

    # user speech — must NOT be logged as assistant
    await feed(TranscriptionFrame(text="I need a room", user_id="customer", timestamp="t"))
    # S2S pushes each assistant token as BOTH an LLMTextFrame (a plain TextFrame
    # here) and a TTSTextFrame — the TTSTextFrame must be ignored, else doubling.
    await feed(TextFrame(text="Sure, "))
    await feed(TTSTextFrame(text="Sure, ", aggregated_by=AggregationType.SENTENCE))
    await feed(TextFrame(text="what dates?"))
    await feed(TTSTextFrame(text="what dates?", aggregated_by=AggregationType.SENTENCE))
    await feed(LLMFullResponseEndFrame())
    await _drain()

    assert logged == ["Sure, what dates?"]
