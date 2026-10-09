"""A live call's media stream from the caller's side, whatever the carrier.

Twilio and Telnyx both stream 8 kHz mu-law as base64 JSON `media` events over
a WebSocket, one 20ms frame every 20ms for as long as the call lasts, silence
included, and play back what the server sends in the same shape. They differ
in the envelope (field names, the start/stop handshake) and in what happens
around the stream (a status callback, a `call.hangup` webhook) — which is all
a carrier's call class (`twilio.TwilioCall`, `telnyx.TelnyxCall`) adds.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from tests.harness.carrier import FakeCarrier
from tests.harness.stubs import tone

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000  # mulaw: one byte per sample
SILENCE = b"\xff" * FRAME_BYTES  # mulaw zero
# Agent audio is a tone; a gap this long between loud frames ends an utterance.
UTTERANCE_GAP_S = 0.3


def mulaw_tone(ms: int) -> list[bytes]:
    """Caller "speech": 20ms mulaw frames of a tone the stub STT hears."""
    ulaw = audioop.lin2ulaw(tone(ms, SAMPLE_RATE), 2)
    return [ulaw[i : i + FRAME_BYTES] for i in range(0, len(ulaw), FRAME_BYTES)]


@dataclass(kw_only=True)
class CarrierCall:
    """One live inbound call: the media stream, from the caller's side.

    A carrier's subclass says how a frame is wrapped (`_media_message`,
    `_send`) and what hanging up looks like (`hang_up`, `finish`).
    """

    carrier: FakeCarrier
    http: httpx.AsyncClient
    call_sid: str
    from_number: str
    to_number: str
    call_id: str
    _ws: websockets.ClientConnection
    _outgoing: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    _loud_at: list[float] = field(default_factory=list)  # agent audio arrival times
    _tasks: list[asyncio.Task] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def say(self, ms: int = 400) -> None:
        """Speak for `ms`: the stub STT transcribes it as the next caller line."""
        for frame in mulaw_tone(ms):
            await self._outgoing.put(frame)

    def agent_utterances(self) -> int:
        """How many separate stretches of agent audio have arrived so far."""
        gaps = sum(
            1
            for a, b in zip(self._loud_at, self._loud_at[1:], strict=False)
            if b - a > UTTERANCE_GAP_S
        )
        return gaps + 1 if self._loud_at else 0

    async def wait_for_agent_speech(
        self, count: int = 1, timeout: float = 10.0
    ) -> None:
        """Until `count` agent utterances have finished playing."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            quiet = (
                self._loud_at and time.monotonic() - self._loud_at[-1] > UTTERANCE_GAP_S
            )
            if self.agent_utterances() >= count and (quiet or self.closed.is_set()):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"heard {self.agent_utterances()} agent utterances, wanted {count}"
        )

    async def hang_up(self) -> None:
        raise NotImplementedError

    async def finish(self, status: str = "completed") -> None:
        raise NotImplementedError

    async def _send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def _media_message(self, chunk: int, payload: str) -> dict[str, Any]:
        raise NotImplementedError

    def _start_streaming(self) -> None:
        self._tasks = [
            asyncio.create_task(self._send_media()),
            asyncio.create_task(self._receive()),
        ]

    async def _stop_streaming(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _send_media(self) -> None:
        """One frame every 20ms, silence unless the caller is speaking."""
        chunk = 0
        start = time.monotonic()
        while True:
            try:
                payload = self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                payload = SILENCE
            chunk += 1
            await self._send(
                self._media_message(chunk, base64.b64encode(payload).decode())
            )
            await asyncio.sleep(
                max(0.0, start + chunk * FRAME_MS / 1000 - time.monotonic())
            )

    async def _receive(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if message.get("event") != "media":
                    continue
                pcm = audioop.ulaw2lin(base64.b64decode(message["media"]["payload"]), 2)
                if audioop.rms(pcm, 2) > 500:
                    self._loud_at.append(time.monotonic())
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed.set()
