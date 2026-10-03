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

import asyncio
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    ControlFrame,
    EndWorkerFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# How long a relayed goodbye waits for gpt-live-1 to finish what it is saying.
# Its fillers are a sentence or two; a reply this long is a model gone on.
_QUIET_TIMEOUT_SECS = 5.0


@dataclass
class HangupArmFrame(ControlFrame):
    """From an ending tool: hang up after the next complete reply.

    `current_reply`: hang up after the reply playing now, or at once if none
    is — for an `openai_live` client delegation that ended the call with
    nothing for gpt-live-1 to voice (#262, `delegation_events`).

    Uninterruptible, like the end it leads to: a caller talking over the
    goodbye must not leave the call recorded as ended and still running.
    """

    current_reply: bool = False
    interruptible: bool = field(default=False, init=False)


class HangupAfterReply(FrameProcessor):
    """Sits right after the LLM; ends the pipeline after the armed reply.

    Armed, it waits for a reply that *starts* after arming and ends there. The
    reply that carried the tool call can finish after the tool armed the gate,
    and ending on it would cut off the goodbye the model has yet to give.
    Armed for the current reply, it ends after the one playing, or at once.

    An `openai_live` client delegate's tool only marks the call as ending
    (`begin_ending`): the live service arms the gate when it relays the
    delegate's answer (`arm_when_quiet`), so a filler gpt-live-1 said
    meanwhile is not taken for the goodbye (#262).
    """

    def __init__(self) -> None:
        super().__init__()
        self._ending = False
        self._armed = False
        self._replying = False  # the reply the call ends after
        self._ended = False
        self._quiet = asyncio.Event()  # no reply is playing
        self._quiet.set()

    @property
    def ending(self) -> bool:
        """An ending tool ran: the call is ending, or has ended."""
        return self._ending

    def begin_ending(self) -> None:
        """The call is ending, and whoever relays the goodbye arms the gate."""
        self._ending = True

    async def arm_when_quiet(self) -> None:
        """Arm for the next reply, once none is playing.

        For a goodbye about to be relayed: sent into a reply still playing, it
        would run on in that reply, and the gate would wait for one that never
        starts.
        """
        try:
            await asyncio.wait_for(self._quiet.wait(), _QUIET_TIMEOUT_SECS)
        except TimeoutError:
            # ponytail: armed anyway; if the goodbye runs on in this reply, the
            # idle guard ends the call. Measure before handling it.
            logger.warning(
                "hangup_gate: still replying after {secs}s, arming anyway",
                secs=_QUIET_TIMEOUT_SECS,
            )
        await self.queue_frame(HangupArmFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, HangupArmFrame):
            self._ending = self._armed = True
            if frame.current_reply:
                self._replying = not self._quiet.is_set()
                if not self._replying:
                    await self._end()
            return  # ours; nothing downstream knows it
        if isinstance(frame, LLMFullResponseStartFrame):
            self._replying = self._replying or self._armed
            self._quiet.clear()
        await self.push_frame(frame, direction)
        if isinstance(frame, LLMFullResponseEndFrame):
            self._quiet.set()
            if self._replying:
                # ponytail: a reply that never comes (LLM down) leaves the call
                # to the idle guard, which ends it after two silences.
                await self._end()

    async def _end(self) -> None:
        self._replying = False
        if not self._ended:
            self._ended = True
            await self.push_frame(EndWorkerFrame(reason="assistant_ended"))
