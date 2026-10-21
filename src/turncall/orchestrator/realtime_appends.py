"""Appended messages reach a Grok or OpenAI Realtime session (#388, #390).

pipecat 1.13's Grok service takes a message the pipeline appends —
`LLMMessagesAppendFrame` — only when the frame itself reaches it: the service
sends the message as a conversation item, the assistant aggregator records it
and pushes the context back upstream, and that upstream push creates the
response. An append queued at the pipeline's source never gets that far. The
user aggregator ahead of the service consumes it, adds the message to the
context and pushes the context *downstream*, which a Grok session already set
up reads as nothing to answer — so the message never reaches xAI and Grok says
nothing.

Two appends that matter go in at the source: the S2S idle check-in
(`CallSession._speak_idle_prompt`) and an eval's typed caller turn (RTVI's
`send-text`). Measured on xAI: both were dropped, Grok silent. pipecat's
OpenAI Realtime service handles the path identically and sent nothing for them
either (#390).

`AppendsAroundTheUserAggregator` hands every append around the user
aggregator, to the processor right after it, so it travels the path pipecat's
service expects. Gemini Live and Nova Sonic read the appended message out of
the downstream context, and gpt-live-1 is checked in with through `say()`, so
the pair is built for `xai` and `openai` only.
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, LLMMessagesAppendFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class _Rejoin(FrameProcessor):
    """Right after the user aggregator: where an append rejoins the pipeline."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class AppendsAroundTheUserAggregator(FrameProcessor):
    """Right before the user aggregator: sends appends past it.

    `rejoin` goes right after the user aggregator. A downstream append is
    queued on it rather than pushed, so the user aggregator never sees it;
    every other frame passes through.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rejoin = _Rejoin()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, LLMMessagesAppendFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self.rejoin.queue_frame(frame, direction)
            return
        await self.push_frame(frame, direction)


def around_the_user_aggregator(
    provider: str,
) -> tuple[list[FrameProcessor], list[FrameProcessor]]:
    """The processors to put before and after the user aggregator, or none."""
    if provider not in ("xai", "openai"):
        return [], []
    bypass = AppendsAroundTheUserAggregator()
    return [bypass], [bypass.rejoin]
