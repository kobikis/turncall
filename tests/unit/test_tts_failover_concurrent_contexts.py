"""The TTS failover against a voice that synthesizes several replies at once.

pipecat 1.17's ElevenLabs service no longer holds the next reply until the
previous one has played: its contexts synthesize concurrently on one socket
and play in order (#499). So the group in front of it is handed reply B while
reply A's audio is still coming out — the reply a failure must replay is no
longer simply the last one asked for. These run the real ElevenLabs service
against a scripted multi-stream socket, with the pipeline factory's write-off
after one silent reply, and check that the backup says exactly what the
caller never heard.
"""

import asyncio
import base64
import json
from dataclasses import dataclass

import pytest
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from websockets.protocol import State

from tests.conftest import creds
from tests.unit.test_stt_tts_failover import _FakeTTS, _watch
from turncall.domain.models import AgentConfig
from turncall.orchestrator.failover import FailoverTTS
from turncall.orchestrator.pipeline_factory import _create_tts_service

pytestmark = pytest.mark.unit

_CHUNK = base64.b64encode(b"\x00\x01" * 480).decode()


@dataclass(frozen=True)
class _Answer:
    """What the socket says for one context: `chunks` of audio, `gap`
    seconds apart, then `isFinal` — or nothing at all, with `final` off."""

    chunks: int = 0
    gap: float = 0.0
    final: bool = True


class _MultiStreamSocket:
    """ElevenLabs' `multi-stream-input` socket, scripted: the n-th context
    opened gets the n-th answer, starting when the service closes it (the end
    of its reply), as the real one finishes a context on `close_context`."""

    def __init__(self, answers: list[_Answer]) -> None:
        self.state = State.OPEN
        self._answers = list(answers)
        self._by_context: dict[str, _Answer] = {}
        self._answered: set[str] = set()
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        context_id = msg.get("context_id")
        if msg.get("close_socket"):
            await self.close()
            return
        if context_id and context_id not in self._by_context and self._answers:
            self._by_context[context_id] = self._answers.pop(0)
        if (
            msg.get("close_context")
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
            await self._inbox.put(
                json.dumps({"audio": _CHUNK, "contextId": context_id})
            )
        if answer.final:
            await self._inbox.put(
                json.dumps({"isFinal": True, "contextId": context_id})
            )

    def __aiter__(self) -> "_MultiStreamSocket":
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


def _elevenlabs(socket: _MultiStreamSocket) -> object:
    """The service as the pipeline factory builds it for an agent with a
    backup: written off after one reply that ends with no audio."""
    config = AgentConfig.model_validate(
        {"tts": {"provider": "elevenlabs", "voice": "21m00Tcm4TlvDq8ikWAM"}}
    )
    service = _create_tts_service(
        config,
        creds(elevenlabs="xi-test"),
        max_consecutive_zero_audio_contexts=1,
        stop_frame_timeout_s=0.2,
    )

    async def connect(_uri: str, **_kwargs: object) -> _MultiStreamSocket:
        return socket

    service._websocket_connect = connect  # type: ignore[attr-defined]
    return service


def _reply(text: str) -> list[Frame]:
    return [LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame()]


async def _run(
    answers: list[_Answer], frames: list[Frame]
) -> tuple[_FakeTTS, list, list[Frame]]:
    socket = _MultiStreamSocket(answers)
    backup = _FakeTTS()
    group = FailoverTTS(
        [("elevenlabs/eleven_flash_v2_5", _elevenlabs(socket)), ("b/2", backup)]
    )
    seen = _watch(group)
    down, _up = await run_test(group, frames_to_send=[*frames, SleepFrame(sleep=0.8)])
    return backup, seen, list(down)


def _audio_chunks(down: list[Frame]) -> int:
    return len([f for f in down if isinstance(f, TTSAudioRawFrame)])


async def test_the_next_reply_starts_while_the_last_is_still_coming_out():
    """The premise, measured: reply B reaches the voice before reply A's
    audio has finished coming out of it, and both still play in order."""
    _backup, seen, down = await _run(
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


async def test_a_reply_that_goes_silent_behind_a_spoken_one_is_replayed():
    """A spoke and finished; B, handed over while A was still coming out,
    comes back silent. A's audio and its end must not be taken for B's."""
    backup, seen, down = await _run(
        [_Answer(chunks=3, gap=0.05), _Answer(chunks=0)],
        [*_reply("Let me check that."), *_reply("Your order ships today.")],
    )

    assert seen and seen[0][:2] == ("elevenlabs/eleven_flash_v2_5", "b/2")
    assert _audio_chunks(down) == 3
    assert backup.spoken == ["Your order ships today."]


async def test_a_silent_reply_ahead_of_another_is_replayed_with_it():
    """A comes back silent while B, already handed over, synthesized fine on
    the same socket: the backup says A, then B — not B alone."""
    backup, seen, down = await _run(
        [_Answer(chunks=0), _Answer(chunks=2, gap=0.01)],
        [*_reply("Let me check that."), *_reply("Your order ships today.")],
    )

    assert seen and seen[0][:2] == ("elevenlabs/eleven_flash_v2_5", "b/2")
    # B's audio from the voice written off never reaches the caller.
    assert _audio_chunks(down) == 0
    assert backup.spoken == ["Let me check that.", "Your order ships today."]


async def test_a_reply_the_socket_never_answers_is_replayed():
    """No `isFinal` and no audio: the context goes quiet, which ElevenLabs
    closes with no stop frame at all, and the reply is still the backup's."""
    backup, seen, _down = await _run(
        [_Answer(chunks=0, final=False)],
        [TTSSpeakFrame("Welcome to Nonna's.")],
    )

    assert seen
    assert backup.spoken == ["Welcome to Nonna's."]


async def test_spoken_replies_owe_the_backup_nothing():
    """Everything played: a later failure replays none of it."""
    backup, seen, down = await _run(
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
