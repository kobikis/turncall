"""A fake TTS and the failover log, for the tests of a speech failover group
(#223, #499).

`FakeTTS` stands in for a pipecat TTS a `FailoverTTS` holds: it says each
sentence as one `TTSTextFrame`, or is refused on the first one.
"""

from __future__ import annotations

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory


class FakeTTS(FrameProcessor):
    """Speaks a reply (or a TTSSpeakFrame) as one TTSTextFrame — or, with
    `fail`, is refused on the first sentence it is asked to say."""

    def __init__(self, fail: ErrorCategory | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.text: list[str] = []
        self.spoken: list[str] = []

    def get_audio_contexts(self) -> list[str]:
        """pipecat's TTS method: it opens no audio context."""
        return []

    async def _speak(self, text: str) -> None:
        if self.fail is not None:
            await self.push_error("tts refused", category=self.fail)
            return
        self.spoken.append(text)
        await self.push_frame(TTSTextFrame(text, text_type="sentence"))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.text.append(frame.text)
            if frame.text.endswith("."):
                # A sentence is ready: the first thing a TTS is asked to say.
                await self._speak("".join(self.text))
                self.text = []
            return
        if isinstance(frame, TTSSpeakFrame):
            await self._speak(frame.text)
            return
        if isinstance(frame, LLMFullResponseEndFrame) and self.text:
            await self._speak("".join(self.text))
            self.text = []
        await self.push_frame(frame, direction)


def watch_failovers(group: object) -> list[tuple[str, str, str]]:
    """Each switch the group makes, as `(from, to, error)`."""
    seen: list[tuple[str, str, str]] = []

    @group.event_handler("on_failover")  # type: ignore[attr-defined]
    async def _on(_g: object, src: str, dst: str, error: ErrorFrame) -> None:
        seen.append((src, dst, error.error))

    return seen
