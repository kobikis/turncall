"""A running Avatar that stops showing never ends the call (#402).

pipecat's HeyGen and Tavus services consume the TTS audio and speak it through
their provider's own leg: the caller hears the Avatar's audio, never the TTS's.
A session the provider refused, or one that stopped mid-call (the LiveAvatar
sandbox's ~1-minute cutoff, a dropped connection), is only a log line to
pipecat, and the service keeps swallowing every reply: the call goes silent.

So each service is subclassed to watch its Avatar, and on losing it to fall
back to voice: from then on the spoken frames pass straight through, as on a
call with no Avatar, and whatever the Avatar's leg still sends is dropped so the
caller never hears a reply twice. One `avatar.unavailable` is recorded with the
reason (`orchestrator/avatar.py`). Nothing switches back.

How a lost Avatar is told, the same way for both providers:
- `session_refused`: setup raised, or left no session behind (pipecat catches
  a refusal and carries on without one); or the Avatar sent no video within
  `JOIN_TIMEOUT_SECS` of the call starting (a session that started with no
  Avatar in it).
- `session_ended`: the Avatar left the provider's room, or its video stopped
  for `STALL_TIMEOUT_SECS`. An Avatar streams video the whole time it is
  there, idle included, so a stall is its session gone.

A reply the Avatar had already taken when it went is lost; the next one is
voice. The session check reads pipecat's private client state for HeyGen,
which has no public one; `tests/unit/test_avatar_unavailable.py` refuses a
session through pipecat's real client, so a rename fails there.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.services.heygen.video import HeyGenVideoService
from pipecat.services.tavus.video import TavusVideoService

from turncall.domain.enums import AvatarUnavailableReason

if TYPE_CHECKING:
    from turncall.orchestrator.avatar import AvatarUnavailability

# A LiveAvatar session's Avatar joins its room in a few seconds; a refusal at
# the API is caught at once, so this bounds only a session that started empty.
JOIN_TIMEOUT_SECS = 20.0
STALL_TIMEOUT_SECS = 5.0
# What the Avatar speaks for the call, and what reaches the caller without it.
_SPOKEN = (TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame)


class AvatarFallback:
    """The watch and the fall back to voice, mixed into a provider's service."""

    def __init__(
        self,
        *args: Any,
        unavailable: AvatarUnavailability | None = None,
        join_timeout: float = JOIN_TIMEOUT_SECS,
        stall_timeout: float = STALL_TIMEOUT_SECS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._unavailable = unavailable
        self._join_timeout = join_timeout
        self._stall_timeout = stall_timeout
        self._voice = False
        self._closing = False
        self._started_at = 0.0
        self._last_video: float | None = None
        self._avatar_participant: str | None = None
        self._watchdog: asyncio.Task[None] | None = None

    def _session_started(self) -> bool:
        """Whether the provider gave the call a session. Per provider."""
        raise NotImplementedError

    async def _fall_back(self, reason: AvatarUnavailableReason) -> None:
        if self._voice or self._closing:
            return
        self._voice = True
        if self._unavailable is not None:
            await self._unavailable.record(reason)

    async def setup(self, setup: FrameProcessorSetup) -> None:
        try:
            await super().setup(setup)  # type: ignore[misc]
        except Exception:
            logger.exception("avatar_setup_failed")
            await self._fall_back(AvatarUnavailableReason.SESSION_REFUSED)
            return
        if not self._session_started():
            await self._fall_back(AvatarUnavailableReason.SESSION_REFUSED)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)  # type: ignore[misc]
        if not self._voice:
            self._started_at = time.monotonic()
            self._watchdog = self.create_task(self._watch())  # type: ignore[attr-defined]

    async def stop(self, frame: EndFrame) -> None:
        await self._closed()
        await super().stop(frame)  # type: ignore[misc]

    async def cancel(self, frame: CancelFrame) -> None:
        await self._closed()
        await super().cancel(frame)  # type: ignore[misc]

    async def _closed(self) -> None:
        # The call is ending: the Avatar leaving now is not news.
        self._closing = True
        if self._watchdog is not None:
            await self.cancel_task(self._watchdog)  # type: ignore[attr-defined]
            self._watchdog = None

    async def _watch(self) -> None:
        tick = min(self._join_timeout, self._stall_timeout) / 4
        while not self._voice:
            await asyncio.sleep(tick)
            now = time.monotonic()
            if self._last_video is None:
                if now - self._started_at >= self._join_timeout:
                    await self._fall_back(AvatarUnavailableReason.SESSION_REFUSED)
            elif now - self._last_video >= self._stall_timeout:
                await self._fall_back(AvatarUnavailableReason.SESSION_ENDED)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if self._voice and isinstance(frame, _SPOKEN):
            # The base's bookkeeping (observers), then on to the caller.
            await FrameProcessor.process_frame(self, frame, direction)  # type: ignore[arg-type]
            await self.push_frame(frame, direction)  # type: ignore[attr-defined]
            return
        await super().process_frame(frame, direction)  # type: ignore[misc]

    async def _on_participant_video_frame(self, *args: Any) -> None:
        if self._voice:
            return
        self._last_video = time.monotonic()
        await super()._on_participant_video_frame(*args)  # type: ignore[misc]

    async def _on_participant_audio_data(self, *args: Any) -> None:
        if self._voice:
            return
        await super()._on_participant_audio_data(*args)  # type: ignore[misc]

    def _avatar_joined(self, participant_id: str) -> None:
        # The first to join is the one pipecat captures: the Avatar.
        if self._avatar_participant is None:
            self._avatar_participant = participant_id

    async def _avatar_left(self, participant_id: str) -> None:
        if participant_id == self._avatar_participant:
            await self._fall_back(AvatarUnavailableReason.SESSION_ENDED)


class WatchedHeyGenVideoService(AvatarFallback, HeyGenVideoService):
    """HeyGen's LiveAvatar, falling back to voice when it stops showing.

    Its reply is cut at the call's barge-in, an `InterruptionFrame`, as Tavus's
    is. pipecat's cut it whenever the caller started speaking: gpt-live-1
    broadcasts no interruption (it hears the caller and decides itself whether
    to stop), nor does a cascade call with barge-in off, so on both the Avatar
    fell silent mid-reply while the agent spoke on.
    """

    def _session_started(self) -> bool:
        # pipecat's client sets it only once LiveAvatar granted the session.
        return getattr(self._client, "_heyGen_session", None) is not None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, UserStartedSpeakingFrame):
            await FrameProcessor.process_frame(self, frame, direction)
            await self.push_frame(frame, direction)
            return
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and not self._voice:
            # pipecat's own cut: drops the queued reply, then listens.
            await self._handle_user_started_speaking()

    async def _on_participant_connected(self, participant_id: str) -> None:
        self._avatar_joined(participant_id)
        await super()._on_participant_connected(participant_id)

    async def _on_participant_disconnected(self, participant_id: str) -> None:
        await super()._on_participant_disconnected(participant_id)
        await self._avatar_left(participant_id)


class WatchedTavusVideoService(AvatarFallback, TavusVideoService):
    """Tavus, falling back to voice when its replica stops showing."""

    def _session_started(self) -> bool:
        # pipecat's client drops the conversation when anything in setup failed.
        return self._client is not None and self._client.conversation_id is not None

    async def _on_participant_joined(self, participant: Any) -> None:
        self._avatar_joined(participant["id"])
        await super()._on_participant_joined(participant)

    async def _on_participant_left(self, participant: Any, reason: Any) -> None:
        await super()._on_participant_left(participant, reason)
        await self._avatar_left(participant["id"])
