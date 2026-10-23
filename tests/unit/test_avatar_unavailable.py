"""`avatar.unavailable`: why a call's Avatar did not show (#402).

Stored on the call's event log, never dispatched (like `metrics.turn`), none in
an eval. Two seams: the build (`build_call_pipeline`), which decides whether a
call can carry the Avatar at all, and the built Avatar service, driven with
frames as the call would drive it while its provider refuses or drops the
session. Either way the call carries on as voice.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    OutputImageRawFrame,
    SpeechOutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from turncall.domain.models import AgentConfig, AvatarConfig
from turncall.orchestrator import pipeline_builder
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit

CREDS = Credentials(
    platform={"deepgram": "k", "openai": "k", "heygen": "k", "tavus": "k"}
)
HEYGEN = {"enabled": True, "provider": "heygen", "avatar_id": "a1"}


def _call_context(**overrides: Any) -> CallContext:
    session = MagicMock(commit=AsyncMock(), get=AsyncMock(return_value=None))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=factory,
        **overrides,
    )


class _EventLog:
    def __init__(self, create: AsyncMock, dispatch: AsyncMock) -> None:
        self._create = create
        self.dispatch = dispatch

    @property
    def avatar(self) -> list[dict[str, Any]]:
        """The `avatar.unavailable` payloads stored, in order."""
        return [
            c.kwargs["payload"]
            for c in self._create.await_args_list
            if c.kwargs["event_type"] == "avatar.unavailable"
        ]


@contextlib.contextmanager
def _event_log() -> Iterator[_EventLog]:
    with (
        patch(
            "turncall.storage.repositories.call_repo.get_next_sequence_number",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "turncall.storage.repositories.call_repo.create_call_event",
            new=AsyncMock(),
        ) as create,
        patch("turncall.events.dispatcher.dispatch_event", new=AsyncMock()) as dispatch,
    ):
        yield _EventLog(create, dispatch)


async def _build(config: AgentConfig, ctx: CallContext, **options: Any) -> MagicMock:
    """Build the call, the pipeline itself stubbed; the stub is returned."""
    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = []
    with (
        patch.object(pipeline_builder, "create_pipeline", return_value=pipeline) as cp,
        patch.object(pipeline_builder, "CallSession"),
        patch.object(
            pipeline_builder, "load_call_credentials", new=AsyncMock(return_value=CREDS)
        ),
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await pipeline_builder.build_call_pipeline(
            config=config,
            transport="T",
            call_context=ctx,
            settings=SimpleNamespace(pipecat=SimpleNamespace(), byom=None),
            session_factory=ctx.session_factory,
            **options,
        )
    return cp


def _agent(avatar: dict[str, Any], **config: Any) -> AgentConfig:
    return AgentConfig.model_validate({"avatar": avatar, **config})


class TestTheBuildSaysWhyItBuiltNoAvatar:
    @pytest.mark.parametrize(
        ("avatar", "config", "carries_video", "reason"),
        [
            # A phone or WhatsApp call: nowhere to show a face.
            (HEYGEN, {}, False, "transport"),
            # Speech-to-speech has no TTS stage for the Avatar to read.
            (
                HEYGEN,
                {"pipeline_mode": "s2s", "s2s": {"provider": "openai"}},
                True,
                "pipeline_mode",
            ),
            ({"enabled": True, "provider": "heygen"}, {}, True, "missing_id"),
            ({"enabled": True, "provider": "tavus"}, {}, True, "missing_id"),
        ],
    )
    async def test_records_the_reason_once(
        self, avatar, config, carries_video, reason
    ) -> None:
        with _event_log() as log:
            cp = await _build(
                _agent(avatar, **config), _call_context(), carries_video=carries_video
            )
        assert log.avatar == [{"provider": avatar["provider"], "reason": reason}]
        assert cp.call_args.kwargs["avatar_enabled"] is False
        log.dispatch.assert_not_awaited()

    async def test_a_browser_call_builds_the_avatar_and_records_nothing(self) -> None:
        with _event_log() as log:
            cp = await _build(_agent(HEYGEN), _call_context(), carries_video=True)
        assert log.avatar == []
        assert cp.call_args.kwargs["avatar_enabled"] is True

    async def test_an_agent_with_no_avatar_records_nothing(self) -> None:
        with _event_log() as log:
            await _build(AgentConfig(), _call_context(), carries_video=False)
        assert log.avatar == []

    async def test_none_in_an_eval(self) -> None:
        with _event_log() as log:
            await _build(
                _agent(HEYGEN),
                _call_context(eval_run_id=uuid.uuid4()),
                carries_video=False,
            )
        assert log.avatar == []


# --- The running Avatar ------------------------------------------------------
#
# A spoken reply, as TTS hands it to the Avatar: the Avatar consumes it and
# speaks it through its own leg; an Avatar that is not there passes it on, so
# the caller hears it as voice.
SPOKEN = [
    TTSStartedFrame(),
    TTSAudioRawFrame(audio=b"\x00\x01" * 320, sample_rate=16000, num_channels=1),
    TTSStoppedFrame(),
]
SPOKEN_TYPES = [TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame]


def _avatar(provider: str = "heygen", **timeouts: float) -> tuple[Any, CallContext]:
    """The call's Avatar service, as the build makes it."""
    from turncall.orchestrator.avatar import AvatarUnavailability
    from turncall.orchestrator.pipeline_factory import _create_avatar_service

    ctx = _call_context()
    config = AvatarConfig(
        enabled=True, provider=provider, avatar_id="a1", replica_id="r1"
    )
    service = _create_avatar_service(
        config, CREDS, AvatarUnavailability(ctx, provider), **timeouts
    )
    return service, ctx


@pytest.fixture
def heygen_refuses(monkeypatch):
    """LiveAvatar answers the session request with an error (a bad avatar id,
    a revoked key): pipecat's client logs it and carries on with no session."""
    from pipecat.services.heygen.api_liveavatar import LiveAvatarApi, LiveAvatarApiError

    async def refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise LiveAvatarApiError("avatar not found", 404, "{}")

    monkeypatch.setattr(LiveAvatarApi, "new_session", refuse)


class TestARefusedSessionCarriesOnAsVoice:
    async def test_heygen(self, heygen_refuses) -> None:
        service, _ = _avatar("heygen")
        with _event_log() as log:
            await run_test(
                service, frames_to_send=SPOKEN, expected_down_frames=SPOKEN_TYPES
            )
        assert log.avatar == [{"provider": "heygen", "reason": "session_refused"}]
        log.dispatch.assert_not_awaited()

    async def test_tavus(self, monkeypatch) -> None:
        from pipecat.transports.tavus.transport import TavusApi

        async def refuse(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("replica not found")

        monkeypatch.setattr(TavusApi, "create_conversation", refuse)
        service, _ = _avatar("tavus")
        with _event_log() as log:
            await run_test(
                service, frames_to_send=SPOKEN, expected_down_frames=SPOKEN_TYPES
            )
        assert log.avatar == [{"provider": "tavus", "reason": "session_refused"}]

    async def test_an_avatar_that_cannot_be_built(self, monkeypatch) -> None:
        from turncall.orchestrator import avatar_watch

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("no LiveKit in this image")

        monkeypatch.setattr(avatar_watch, "WatchedHeyGenVideoService", broken)
        with _event_log() as log:
            service, _ = _avatar("heygen")
            for _ in range(5):
                await asyncio.sleep(0)
        assert service is None
        assert log.avatar == [{"provider": "heygen", "reason": "session_refused"}]


@pytest.fixture
def heygen_session(monkeypatch):
    """LiveAvatar grants a session; its room is the test's to play."""
    from pipecat.services.heygen.api_liveavatar import LiveAvatarApi
    from pipecat.services.heygen.client import HeyGenClient

    granted = SimpleNamespace(
        session_id="s1", ws_url="wss://x", livekit_url="wss://y", access_token="t"
    )
    monkeypatch.setattr(LiveAvatarApi, "new_session", AsyncMock(return_value=granted))
    monkeypatch.setattr(LiveAvatarApi, "close_session", AsyncMock())
    # The connections themselves: nothing for a unit test to reach.
    monkeypatch.setattr(HeyGenClient, "_ws_connect", AsyncMock())
    monkeypatch.setattr(HeyGenClient, "_livekit_connect", AsyncMock())


@pytest.fixture(autouse=True)
async def _close_shared_http_session():
    yield
    from turncall.adapters.aiohttp_client import close_aiohttp_session

    await close_aiohttp_session()


class _AvatarRoom:
    """The Avatar in its provider's room: joins, streams video, leaves."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._streaming = False
        self._task: Any = None

    def _frame(self) -> Any:
        from pipecat.frames.frames import ImageRawFrame

        return ImageRawFrame(image=b"\x00" * 12, size=(2, 2), format="RGB")

    async def _stream(self, every: float) -> None:
        while self._streaming:
            await self._service._on_participant_video_frame(self._frame())
            await asyncio.sleep(every)

    async def join(self, every: float = 0.02) -> None:
        await self._service._on_participant_connected("avatar")
        self._streaming = True
        self._task = asyncio.create_task(self._stream(every))

    async def freeze(self) -> None:
        self._streaming = False
        await self._task

    async def leave(self) -> None:
        await self.freeze()
        await self._service._on_participant_disconnected("avatar")

    async def speak_late(self) -> None:
        """What the Avatar's leg still sends after the call gave up on it."""
        from pipecat.frames.frames import AudioRawFrame

        await self._service._on_participant_video_frame(self._frame())
        await self._service._on_participant_audio_data(
            AudioRawFrame(audio=b"\x00\x01" * 160, sample_rate=16000, num_channels=1)
        )


def _heard(down: Any) -> list[type]:
    """What reached the caller of the reply and of the Avatar's own leg."""
    kinds = (*SPOKEN_TYPES, OutputImageRawFrame, SpeechOutputAudioRawFrame)
    return [type(f) for f in down if isinstance(f, kinds)]


async def _during(room_play: Any, frames: list[Any], service: Any) -> Any:
    """Run the call's frames while the room plays its part."""
    play = asyncio.create_task(room_play())
    down, _ = await run_test(service, frames_to_send=frames)
    await play
    return down


class TestAnAvatarThatShows:
    async def test_speaks_the_reply_and_records_nothing(self, heygen_session) -> None:
        service, _ = _avatar("heygen", join_timeout=1.0, stall_timeout=1.0)
        room = _AvatarRoom(service)

        async def plays() -> None:
            await asyncio.sleep(0.05)
            await room.join()
            await asyncio.sleep(0.6)
            await room.freeze()

        with _event_log() as log:
            down = await _during(
                plays, [SleepFrame(0.2), *SPOKEN, SleepFrame(0.5)], service
            )
        heard = _heard(down)
        # The reply went to the Avatar, which speaks it through its own leg.
        assert TTSAudioRawFrame not in heard
        assert OutputImageRawFrame in heard
        assert log.avatar == []


class TestAnAvatarThatStops:
    async def test_its_video_stalling_ends_it_and_the_call_goes_on_as_voice(
        self, heygen_session
    ) -> None:
        service, _ = _avatar("heygen", join_timeout=0.2, stall_timeout=0.1)
        room = _AvatarRoom(service)

        async def plays() -> None:
            await asyncio.sleep(0.05)
            await room.join()
            await asyncio.sleep(0.15)
            await room.freeze()  # the sandbox's cutoff
            await asyncio.sleep(0.5)
            await room.speak_late()

        with _event_log() as log:
            down = await _during(
                plays, [SleepFrame(1.0), *SPOKEN, SleepFrame(0.1)], service
            )
        heard = _heard(down)
        assert heard[-3:] == SPOKEN_TYPES
        # Nothing from the Avatar's leg after the call gave up on it.
        assert SpeechOutputAudioRawFrame not in heard
        assert OutputImageRawFrame not in heard[heard.index(TTSStartedFrame) :]
        assert log.avatar == [{"provider": "heygen", "reason": "session_ended"}]

    async def test_leaving_its_room_ends_it(self, heygen_session) -> None:
        service, _ = _avatar("heygen", join_timeout=5, stall_timeout=5)
        room = _AvatarRoom(service)

        async def plays() -> None:
            await asyncio.sleep(0.05)
            await room.join()
            await asyncio.sleep(0.1)
            await room.leave()

        with _event_log() as log:
            down = await _during(
                plays, [SleepFrame(0.3), *SPOKEN, SleepFrame(0.05)], service
            )
        assert _heard(down)[-3:] == SPOKEN_TYPES
        assert log.avatar == [{"provider": "heygen", "reason": "session_ended"}]

    async def test_one_that_never_joins_was_refused(self, heygen_session) -> None:
        service, _ = _avatar("heygen", join_timeout=0.1, stall_timeout=0.1)
        with _event_log() as log:
            await run_test(
                service,
                frames_to_send=[SleepFrame(0.5), *SPOKEN],
                expected_down_frames=SPOKEN_TYPES,
            )
        assert log.avatar == [{"provider": "heygen", "reason": "session_refused"}]


class TestNoneInAnEval:
    async def test_a_refused_session_records_nothing(self, heygen_refuses) -> None:
        from turncall.orchestrator.avatar import AvatarUnavailability
        from turncall.orchestrator.pipeline_factory import _create_avatar_service

        ctx = _call_context(eval_run_id=uuid.uuid4())
        service = _create_avatar_service(
            AvatarConfig(enabled=True, avatar_id="a1"),
            CREDS,
            AvatarUnavailability(ctx, "heygen"),
        )
        with _event_log() as log:
            await run_test(
                service, frames_to_send=SPOKEN, expected_down_frames=SPOKEN_TYPES
            )
        assert log.avatar == []
