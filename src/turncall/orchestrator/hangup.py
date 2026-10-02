"""End the call after the agent's goodbye (#240).

`end_call` / `do_not_call` arm the gate; the model then answers the tool
result — its goodbye — and the gate ends the pipeline once that reply is
complete. Keyed on the reply rather than on the tool call because models do
not reliably say goodbye *and* call a tool in one response: gpt-4o-mini said
goodbye, waited, and called `end_call` alone on the caller's next "bye".

The `EndWorkerFrame` goes downstream behind the reply, and pipecat answers it
with an `EndFrame` from the source: the TTS drains its pending audio on that
frame and the output transport plays out its queue before closing. Closing is
the hangup — a Twilio stream with no TwiML after `<Connect>` ends the call, a
WebRTC peer is disconnected — and `finalize_call` completes the row.

The goodbye is final, on purpose. The `EndFrame` reaches `transport.input()`
first, which stops reading audio while the goodbye is still playing: the
caller cannot interrupt it, and what they say over it is neither transcribed
nor recorded. Listening on until it had played would raise the question of
what talking over it means, and a goodbye that can be taken back would need
the end recorded at the hangup rather than at the tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipecat.frames.frames import (
    ControlFrame,
    EndWorkerFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


@dataclass
class HangupArmFrame(ControlFrame):
    """From an ending tool: hang up after the next complete reply.

    Uninterruptible, like the end it leads to: a caller talking over the
    goodbye must not leave the call recorded as ended and still running.
    """

    interruptible: bool = field(default=False, init=False)


class HangupAfterReply(FrameProcessor):
    """Sits right after the LLM; ends the pipeline after the armed reply.

    Armed, it waits for a reply that *starts* after arming and ends there. The
    reply that carried the tool call can finish after the tool armed the gate,
    and ending on it would cut off the goodbye the model has yet to give.
    """

    def __init__(self) -> None:
        super().__init__()
        self._armed = False
        self._replying = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, HangupArmFrame):
            self._armed = True
            return  # ours; nothing downstream knows it
        if self._armed and isinstance(frame, LLMFullResponseStartFrame):
            self._replying = True
        await self.push_frame(frame, direction)
        if self._replying and isinstance(frame, LLMFullResponseEndFrame):
            self._armed = self._replying = False
            # ponytail: a reply that never comes (LLM down) leaves the call to
            # the idle guard, which ends it after two silences.
            await self.push_frame(EndWorkerFrame(reason="assistant_ended"))
