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
  - TTS: every reply handed over and not yet played out. A TTS refused on its
    first sentence (often the greeting) otherwise loses the reply's opening,
    or all of it.

- **a stalled STT is written off.** One that takes the caller's audio and never
  answers reports no error, so pipecat never moves the call off it. The group
  reads it from the call instead (`_SILENT_TURNS`). (A stalled TTS pipecat does
  catch, as replies that end with no audio; the pipeline factory has it give up
  after one when there is a backup to move to.)
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
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
    TTSAudioRawFrame,
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

# A silent turn: the caller spoke, stopped, and waited this long with nothing
# transcribed — a final transcript usually lands well under a second after.
_SILENT_TURN_S = 3.0
# This many in a row writes the STT off. One is a cough or a door.
# ponytail: a noisy line with no speech can trip it too; the cost is the backup
# STT for the rest of the call, which the caller does not hear.
_SILENT_TURNS = 2


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
        # Anything transcribed, interim or final, since the last silent-turn
        # check; consecutive silent turns; the check pending after a stop.
        self._heard = False
        self._silent_turns = 0
        self._silence_check: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InputAudioRawFrame):
            self._unheard.append(frame)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._speech = self._speaking = True
            await self._cancel_silence_check()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
            await self._cancel_silence_check()
            self._silence_check = self.create_task(self._check_silent_turn())
        await super().process_frame(frame, direction)

    async def cleanup(self) -> None:
        await self._cancel_silence_check()
        await super().cleanup()

    async def _cancel_silence_check(self) -> None:
        if self._silence_check:
            await self.cancel_task(self._silence_check)
            self._silence_check = None

    async def _check_silent_turn(self) -> None:
        await asyncio.sleep(_SILENT_TURN_S)
        self._silence_check = None
        self._silent_turns = 0 if self._heard else self._silent_turns + 1
        self._heard = False
        if self._silent_turns < _SILENT_TURNS:
            return
        active = self.strategy.active_service
        later = self.strategy.services[self.strategy.services.index(active) + 1 :]
        if not any(s.is_usable for s in later):
            return  # nobody to move to: writing it off would only end the call
        self._silent_turns = 0
        await active.push_error(
            f"no transcript for {_SILENT_TURNS} turns in a row",
            force_treat_as_permanent=True,
        )

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            self._heard = True
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


@dataclass
class _Reply:
    """One reply handed to the TTS: the frames that asked for it, whether any
    of its audio has come out, and whether a backup was handed it again."""

    frames: list[Frame]
    heard: bool = False
    replayed: bool = False


class FailoverTTS(FailoverGroup, ServiceSwitcher[ServiceSwitcherStrategyFailover]):
    """An agent's TTS and its backups, in order. `ttss` are `(label, service)`.

    A voice may take the next reply before the last has finished coming out of
    it: pipecat 1.17's ElevenLabs synthesizes several replies at once, as
    Cartesia already did, and plays them in order (#499). So the group keeps
    every reply handed over and not yet played out, oldest first — the one
    whose audio is coming out is the oldest — and a failure replays them all.
    """

    role = "tts"

    def __init__(self, ttss: list[tuple[str, Any]]) -> None:
        super().__init__([s for _, s in ttss], ServiceSwitcherStrategyFailover)
        self._init_group(ttss)
        self._replies: deque[_Reply] = deque()
        # The LLM reply still being handed over, which its text joins.
        self._open: _Reply | None = None
        # Contexts a written-off voice had already taken (see `_silence`).
        self._silenced: set[str] = set()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if direction is FrameDirection.DOWNSTREAM:
            self._note_reply(frame)
        await super().process_frame(frame, direction)

    def _note_reply(self, frame: Frame) -> None:
        if isinstance(frame, (LLMFullResponseStartFrame, TTSSpeakFrame)):
            if getattr(frame, "skip_tts", False):
                return
            reply = _Reply([frame])
            self._replies.append(reply)
            self._open = reply if isinstance(frame, LLMFullResponseStartFrame) else None
        elif isinstance(frame, InterruptionFrame):
            # Cut off by the caller: nothing handed over is wanted any more.
            self._replies.clear()
            self._open = None
        elif (
            self._open is not None
            and isinstance(frame, (TextFrame, LLMFullResponseEndFrame))
            and not isinstance(frame, TranscriptionFrame)
            and not frame.skip_tts
        ):
            self._open.frames.append(frame)
            if isinstance(frame, LLMFullResponseEndFrame):
                self._close_open()

    def _close_open(self) -> None:
        """A reply with nothing to say (only tool calls) opens no context and
        sends no stop: kept, it would take the next reply's audio for its own."""
        reply, self._open = self._open, None
        spoken = any(
            isinstance(f, TextFrame) and f.text.strip() for f in reply.frames[1:]
        )
        if not spoken and reply in self._replies:
            self._replies.remove(reply)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if direction is FrameDirection.DOWNSTREAM:
            if self._silenced_frame(frame):
                return
            self._note_played(frame)
        await super().push_frame(frame, direction)

    def _note_played(self, frame: Frame) -> None:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._played_out_before(frame)
        elif isinstance(frame, TTSAudioRawFrame) and self._replies:
            self._replies[0].heard = True
        elif isinstance(frame, TTSStoppedFrame) and self._replies:
            # The oldest reply has been spoken; a later failure owes it
            # nothing. A stop with no audio before it is pipecat closing a
            # context that timed out silent — the failure that writes a
            # stalled TTS off comes right after it, and that reply is the one
            # to replay from.
            if self._replies[0].heard:
                self._replies.popleft()

    def _played_out_before(self, start: LLMFullResponseStartFrame) -> None:
        """A reply's start comes out behind every earlier reply's audio, so
        those have finished; one that ended silent was dealt with by the
        failure that followed it. A replayed reply's start may be the
        written-off voice's, out ahead of the backup: it proves nothing."""
        at = next(
            (
                i
                for i, reply in enumerate(self._replies)
                if reply.frames[0] is start and not reply.replayed
            ),
            0,
        )
        for _ in range(at):
            self._replies.popleft()

    def _silenced_frame(self, frame: Frame) -> bool:
        """Audio and text of a context a written-off voice had taken before it
        failed — the next reply, synthesized while the silent one played.
        pipecat's switcher gates a member's input and its upstream output,
        never its downstream output, so it would still play, beside the
        backup's replay of the same reply.
        `test_tts_failover_concurrent_contexts.py::test_pipecat_still_needs_it`
        fails once pipecat gates it."""
        context_id = getattr(frame, "context_id", None)
        if context_id is None:
            return False
        if context_id not in self._silenced and any(
            context_id in _open_contexts(s) for s in self._failed_members()
        ):
            self._silenced.add(context_id)
        return context_id in self._silenced

    def _failed_members(self) -> list[Any]:
        return [s for s in self.strategy.services if id(s) in self._failed]

    async def _replay(self, service: Any) -> None:
        """Every reply not yet played out, from the oldest. The original
        frames, not copies: the switcher passes a frame id out once and
        observers report one once, so the bracket the failed member already
        let through is not repeated.

        ponytail: a TTS that spoke half a reply before dying has it said again
        in full; the failures this is for (a refused key, an unknown voice, an
        outage) die before the first audio."""
        for failed in self._failed_members():
            self._silenced.update(_open_contexts(failed))
        for reply in list(self._replies):
            reply.replayed = True
            reply.heard = False
            for frame in list(reply.frames):
                await service.queue_frame(frame)


def _open_contexts(service: Any) -> list[str]:
    """The audio contexts a pipecat TTS holds, queued or playing. Every TTS
    a group holds is pipecat's: a rename fails here, not silently."""
    return list(service.get_audio_contexts())
