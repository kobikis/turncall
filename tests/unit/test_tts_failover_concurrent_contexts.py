"""The TTS failover against a voice that synthesizes several replies at once.

Cartesia always synthesized the next reply while the last was still coming
out, and pipecat 1.17's ElevenLabs does too: contexts synthesize concurrently
on one socket and play in order (#499). So the group in front of either is
handed reply B while reply A's audio is still coming out — the reply a failure
must replay is no longer simply the last one asked for. The group kept only
the last reply, which was wrong for Cartesia before 1.17 and became wrong for
ElevenLabs with it. These run the real ElevenLabs and Cartesia services against
a scripted socket, with the pipeline factory's write-off after one silent
reply, and check that the backup says exactly what the caller never heard.
"""

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.utils.errors import ErrorCategory
from websockets.protocol import State

from tests.conftest import creds
from tests.failover_fakes import FakeTTS, watch_failovers
from turncall.domain.models import AgentConfig
from turncall.orchestrator.failover import FailoverTTS
from turncall.orchestrator.pipeline_factory import _create_tts_service

pytestmark = pytest.mark.unit

_CHUNK = base64.b64encode(b"\x00\x01" * 480).decode()


@dataclass(frozen=True)
class _Answer:
    """What the socket says for one context: `chunks` of audio, `gap`
    seconds apart, then its end — or nothing at all, with `final` off."""

    chunks: int = 0
    gap: float = 0.0
    final: bool = True


@dataclass(frozen=True)
class _Protocol:
    """How a voice's socket names a context, closes one, and answers it."""

    closes_context: Callable[[dict[str, Any]], bool]
    closes_socket: Callable[[dict[str, Any]], bool]
    audio: Callable[[str], dict[str, Any]]
    end: Callable[[str], dict[str, Any]]


# ElevenLabs' `multi-stream-input`: `close_context` ends a reply, `isFinal`
# answers its end.
_ELEVENLABS = _Protocol(
    closes_context=lambda msg: bool(msg.get("close_context")),
    closes_socket=lambda msg: bool(msg.get("close_socket")),
    audio=lambda ctx: {"audio": _CHUNK, "contextId": ctx},
    end=lambda ctx: {"isFinal": True, "contextId": ctx},
)
# Cartesia's: a `continue: false` flush ends a reply, `done` answers its end.
_CARTESIA = _Protocol(
    closes_context=lambda msg: msg.get("continue") is False,
    closes_socket=lambda _msg: False,
    audio=lambda ctx: {"type": "chunk", "data": _CHUNK, "context_id": ctx},
    end=lambda ctx: {"type": "done", "context_id": ctx},
)


class _ScriptedSocket:
    """A voice's socket, scripted: the n-th context opened gets the n-th
    answer, starting when the service closes it (the end of its reply), as the
    real one finishes a context then."""

    def __init__(self, protocol: _Protocol, answers: list[_Answer]) -> None:
        self.state = State.OPEN
        self._protocol = protocol
        self._answers = list(answers)
        self._by_context: dict[str, _Answer] = {}
        self._answered: set[str] = set()
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        if self._protocol.closes_socket(msg):
            await self.close()
            return
        context_id = msg.get("context_id")
        if context_id and context_id not in self._by_context and self._answers:
            self._by_context[context_id] = self._answers.pop(0)
        if (
            self._protocol.closes_context(msg)
            and context_id in self._by_context
            and context_id not in self._answered
        ):
            self._answered.add(context_id)
            task = asyncio.create_task(self._answer(context_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _answer(self, context_id: str) -> None:
        answer = self._by_context[context_id]
        for _ in range(answer.chunks):
            await asyncio.sleep(answer.gap)
            await self._inbox.put(json.dumps(self._protocol.audio(context_id)))
        if answer.final:
            await self._inbox.put(json.dumps(self._protocol.end(context_id)))

    def __aiter__(self) -> "_ScriptedSocket":
        return self

    async def __anext__(self) -> str:
        message = await self._inbox.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def wait_closed(self) -> None:
        return None

    async def close(self) -> None:
        if self.state is not State.CLOSED:
            self.state = State.CLOSED
            for task in list(self._tasks):
                task.cancel()
            await self._inbox.put(None)


@dataclass(frozen=True)
class _Voice:
    label: str
    tts: dict[str, Any]
    protocol: _Protocol


_VOICES = [
    _Voice(
        "elevenlabs/eleven_flash_v2_5",
        {"provider": "elevenlabs", "voice": "21m00Tcm4TlvDq8ikWAM"},
        _ELEVENLABS,
    ),
    _Voice(
        "cartesia/sonic-3.6",
        {"provider": "cartesia", "voice": "a0e99841-438c-4a64-b679-ae501e7d6091"},
        _CARTESIA,
    ),
]


@pytest.fixture(params=_VOICES, ids=lambda v: v.tts["provider"])
def voice(request: pytest.FixtureRequest) -> _Voice:
    return request.param


def _service(voice: _Voice, socket: _ScriptedSocket) -> object:
    """The service as the pipeline factory builds it for an agent with a
    backup: written off after one reply that ends with no audio."""
    config = AgentConfig.model_validate({"tts": voice.tts})
    service = _create_tts_service(
        config,
        creds(elevenlabs="xi-test", cartesia="sk_car_test"),
        max_consecutive_zero_audio_contexts=1,
        stop_frame_timeout_s=0.2,
    )

    async def connect(_uri: str, **_kwargs: object) -> _ScriptedSocket:
        return socket

    service._websocket_connect = connect  # type: ignore[attr-defined]
    return service


def _reply(text: str) -> list[Frame]:
    return [LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame()]


async def _run(
    voice: _Voice, answers: list[_Answer], frames: list[Frame]
) -> tuple[FakeTTS, list, list[Frame]]:
    socket = _ScriptedSocket(voice.protocol, answers)
    backup = FakeTTS()
    group = FailoverTTS([(voice.label, _service(voice, socket)), ("b/2", backup)])
    seen = watch_failovers(group)
    down, _up = await run_test(group, frames_to_send=[*frames, SleepFrame(sleep=0.8)])
    return backup, seen, list(down)


def _audio_chunks(down: list[Frame]) -> int:
    return len([f for f in down if isinstance(f, TTSAudioRawFrame)])


async def test_the_next_reply_starts_while_the_last_is_still_coming_out(voice: _Voice):
    """The premise, measured: reply B reaches the voice before reply A's
    audio has finished coming out of it, and both still play in order."""
    _backup, seen, down = await _run(
        voice,
        [_Answer(chunks=3, gap=0.05), _Answer(chunks=2, gap=0.01)],
        [*_reply("Let me check that."), *_reply("Your order ships today.")],
    )

    assert seen == []
    kinds = [
        type(f).__name__
        for f in down
        if isinstance(f, (LLMFullResponseStartFrame, TTSAudioRawFrame))
    ]
    # A's three chunks all come out before B's start: the order holds.
    assert kinds[:5] == [
        "LLMFullResponseStartFrame",
        "TTSAudioRawFrame",
        "TTSAudioRawFrame",
        "TTSAudioRawFrame",
        "LLMFullResponseStartFrame",
    ]


async def test_a_reply_that_goes_silent_behind_a_spoken_one_is_replayed(voice: _Voice):
    """A spoke and finished; B, handed over while A was still coming out,
    comes back silent. A's audio and its end must not be taken for B's."""
    backup, seen, down = await _run(
        voice,
        [_Answer(chunks=3, gap=0.05), _Answer(chunks=0)],
        [*_reply("Let me check that."), *_reply("Your order ships today.")],
    )

    assert seen and seen[0][:2] == (voice.label, "b/2")
    assert _audio_chunks(down) == 3
    assert backup.spoken == ["Your order ships today."]


async def test_a_silent_reply_ahead_of_another_is_replayed_with_it(voice: _Voice):
    """A comes back silent while B, already handed over, synthesized fine on
    the same socket: the backup says A, then B — not B alone."""
    backup, seen, down = await _run(
        voice,
        [_Answer(chunks=0), _Answer(chunks=2, gap=0.01)],
        [*_reply("Let me check that."), *_reply("Your order ships today.")],
    )

    assert seen and seen[0][:2] == (voice.label, "b/2")
    # B's audio from the voice written off never reaches the caller.
    assert _audio_chunks(down) == 0
    assert backup.spoken == ["Let me check that.", "Your order ships today."]


async def test_a_reply_the_socket_never_answers_is_replayed(voice: _Voice):
    """No audio and no end: the context goes quiet, which the service
    closes with no stop frame at all, and the reply is still the backup's."""
    backup, seen, _down = await _run(
        voice,
        [_Answer(chunks=0, final=False)],
        [TTSSpeakFrame("Welcome to Nonna's.")],
    )

    assert seen
    assert backup.spoken == ["Welcome to Nonna's."]


async def test_spoken_replies_owe_the_backup_nothing(voice: _Voice):
    """Everything played: a later failure replays none of it."""
    backup, seen, down = await _run(
        voice,
        [_Answer(chunks=2, gap=0.02), _Answer(chunks=2, gap=0.02), _Answer(chunks=0)],
        [
            TTSSpeakFrame("Welcome to Nonna's."),
            *_reply("Let me check that."),
            SleepFrame(sleep=0.3),
            *_reply("Your table is booked."),
        ],
    )

    assert _audio_chunks(down) == 4
    assert seen
    assert backup.spoken == ["Your table is booked."]


class _PlaysOnAfterItFails(FrameProcessor):
    """A voice refused on the reply it is handed, whose audio for a context it
    took earlier still comes out afterwards."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, TTSSpeakFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_error("refused", category=ErrorCategory.AUTHENTICATION)
        await self.push_frame(
            TTSAudioRawFrame(
                audio=b"\x00\x01" * 480,
                sample_rate=24000,
                num_channels=1,
                context_id="taken-earlier",
            )
        )


async def test_pipecat_still_needs_it():
    """pipecat's switcher gates a member's input and its upstream output,
    never its downstream output: a voice it has moved off still plays what it
    had synthesized. `FailoverTTS._silenced_frame` drops that audio by its
    context; when this fails, pipecat drops it, and that can go."""
    failed = _PlaysOnAfterItFails()
    switcher = ServiceSwitcher([failed, FakeTTS()], ServiceSwitcherStrategyFailover)

    down, _up = await run_test(switcher, frames_to_send=[TTSSpeakFrame("Hi.")])

    assert switcher.strategy.active_service is not failed
    played = [f.context_id for f in down if isinstance(f, TTSAudioRawFrame)]
    assert played == ["taken-earlier"]
