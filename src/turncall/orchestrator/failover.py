"""Failover groups (#222, #223): the backup providers a call moves to.

pipecat's `ServiceSwitcher` with its failover strategy does the switching: only
an error that leaves the active service unusable moves the call on, in list
order. What it leaves to the application is what this module adds, once for
every role:

- **no switching back.** pipecat makes a service usable again on any settings
  change, so a failed member is held down for the rest of the call — otherwise
  the next failover could wrap round to it, and the caller would hear the voice
  change twice.
- **an `on_failover` event** naming both members, which `CallSession` records.
- **a replay** of the work the failed member took with it, so the call carries
  on where it was rather than where the backup happened to pick it up:

  - STT: the caller's speech nobody transcribed yet. Without it, a segmented
    STT refused at the end of the caller's first sentence (a bad key on an
    HTTP provider) loses that sentence, and the line sits silent until the
    caller repeats it.
  - TTS: the reply being spoken. A TTS refused on its first sentence (often the
    greeting) otherwise loses the reply's opening, or all of it.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.processors.frame_processor import FrameDirection

# Set on each member so a handler that only holds the member (a tool call's
# `params.llm`) can reach the whole group.
_GROUP_ATTR = "_turncall_failover_group"

# The caller's speech kept for an STT replay, in input frames: at the 20ms
# frames every transport sends, 30s — longer than anyone talks in one turn,
# short enough that a long silence does not become a long replay.
_STT_REPLAY_FRAMES = 1500


class FailoverGroup:
    """What every role's group adds to pipecat's switcher. A mixin: it sits in
    front of a `ServiceSwitcher` subclass, and `_init_group` runs after the
    switcher's own `__init__`.

    Event handlers: ``on_failover(group, from_label, to_label, error_frame)``.
    """

    role: str = ""

    def _init_group(self, members: list[tuple[str, Any]]) -> None:
        self._labels = {id(service): label for label, service in members}
        # Members this call has given up on (see the module docstring).
        self._failed: set[int] = set()
        for _, service in members:
            setattr(service, _GROUP_ATTR, self)
            service.add_event_handler("on_usable_changed", self._keep_failed_down)
        self._register_event_handler("on_failover")  # type: ignore[attr-defined]

    async def _keep_failed_down(self, service: Any, is_usable: bool) -> None:
        if is_usable and id(service) in self._failed:
            await service.set_usable(False)

    def label(self, service: Any) -> str:
        """`provider/model` of a member, as the call's event log names it."""
        return self._labels[id(service)]

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        strategy = self.strategy  # type: ignore[attr-defined]
        before = strategy.active_service
        await super().push_frame(frame, direction)  # type: ignore[misc]
        after = strategy.active_service
        if after is before or not isinstance(frame, ErrorFrame):
            return
        self._failed.add(id(before))
        await self._call_event_handler(  # type: ignore[attr-defined]
            "on_failover", self.label(before), self.label(after), frame
        )
        await self._replay(after)

    async def _replay(self, service: Any) -> None:
        """Hand the new member what the failed one took with it."""


class FailoverSTT(FailoverGroup, ServiceSwitcher[ServiceSwitcherStrategyFailover]):
    """An agent's STT and its backups, in order. `stts` are `(label, service)`."""

    role = "stt"

    def __init__(self, stts: list[tuple[str, Any]]) -> None:
        super().__init__([s for _, s in stts], ServiceSwitcherStrategyFailover)
        self._init_group(stts)
        self._unheard: deque[InputAudioRawFrame] = deque(maxlen=_STT_REPLAY_FRAMES)
        # VAD heard the caller since the last final transcript; and is hearing
        # them now. VAD runs downstream, in the user aggregator, which
        # broadcasts its frames back up through here.
        self._speech = False
        self._speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InputAudioRawFrame):
            self._unheard.append(frame)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._speech = self._speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
        await super().process_frame(frame, direction)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if isinstance(frame, TranscriptionFrame) and not isinstance(
            frame, InterimTranscriptionFrame
        ):
            # Everything said so far has been heard.
            self._unheard.clear()
            self._speech = self._speaking
        await super().push_frame(frame, direction)

    async def _replay(self, service: Any) -> None:
        """The untranscribed speech, bracketed by VAD the way a segmented STT
        (OpenAI, ElevenLabs) needs to hear a segment; a streaming one ignores
        the brackets. Sent upstream, the way VAD reaches an STT, so nothing
        downstream mistakes them for the caller starting a new turn. The audio
        is the original frames: the switcher passes a frame id out once, so
        the caller's audio is not delivered downstream twice."""
        if not self._speech or not self._unheard:
            return
        await service.queue_frame(
            VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM
        )
        for audio in list(self._unheard):
            await service.queue_frame(audio)
        if not self._speaking:
            await service.queue_frame(
                VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM
            )


class FailoverTTS(FailoverGroup, ServiceSwitcher[ServiceSwitcherStrategyFailover]):
    """An agent's TTS and its backups, in order. `ttss` are `(label, service)`."""

    role = "tts"

    def __init__(self, ttss: list[tuple[str, Any]]) -> None:
        super().__init__([s for _, s in ttss], ServiceSwitcherStrategyFailover)
        self._init_group(ttss)
        # The reply being spoken, as the frames that asked for it.
        self._reply: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if direction is FrameDirection.DOWNSTREAM:
            self._note_reply(frame)
        await super().process_frame(frame, direction)

    def _note_reply(self, frame: Frame) -> None:
        if isinstance(frame, (LLMFullResponseStartFrame, TTSSpeakFrame)):
            if not getattr(frame, "skip_tts", False):
                self._reply = [frame]
        elif isinstance(frame, InterruptionFrame):
            # Cut off by the caller: nothing of it is wanted any more.
            self._reply = []
        elif (
            self._reply
            and isinstance(frame, (TextFrame, LLMFullResponseEndFrame))
            and not isinstance(frame, TranscriptionFrame)
            and not frame.skip_tts
        ):
            self._reply.append(frame)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if isinstance(frame, TTSStoppedFrame):
            # The reply has been synthesized; a later failure owes it nothing.
            self._reply = []
        await super().push_frame(frame, direction)

    async def _replay(self, service: Any) -> None:
        """The reply from its start. The original frames, not copies: the
        switcher passes a frame id out once and observers report one once, so
        the bracket the failed member already let through is not repeated.

        ponytail: a TTS that spoke half a reply before dying has it said again
        in full; the failures this is for (a refused key, an unknown voice, an
        outage) die before the first audio."""
        for frame in list(self._reply):
            await service.queue_frame(frame)
