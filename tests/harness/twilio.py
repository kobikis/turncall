"""Twilio's side of a call: signed webhooks and a Media Streams client.

What Twilio does on an inbound call, in order: POST the number's voice URL
(signed), read the TwiML, open the `<Stream>` WebSocket, send `connected` and
`start`, then one 20ms mulaw `media` frame every 20ms for as long as the call
lasts — silence included — and finally `stop`. When the stream ends the call
ends, and the number's status callback hears `completed`. `TwilioCall` does
exactly that at real-time pace.

A second carrier gets a sibling module with the same `place_inbound_call` /
call-object shape (`say`, `wait_for_agent_speech`, `hang_up`, `closed`), so a
contract test can run one script against each.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import secrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
import websockets
from twilio.request_validator import RequestValidator

from tests.harness.carrier import FakeCarrier
from tests.harness.stubs import tone

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000  # mulaw: one byte per sample
SILENCE = b"\xff" * FRAME_BYTES  # mulaw zero
# Agent audio is a tone; a gap this long between loud frames ends an utterance.
UTTERANCE_GAP_S = 0.3


def sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    """`X-Twilio-Signature` for a POST of `params` to `url`."""
    return RequestValidator(auth_token).compute_signature(url, params)


async def post_signed(
    http: httpx.AsyncClient, carrier: FakeCarrier, url: str, params: dict[str, str]
) -> httpx.Response:
    """POST a form the way Twilio does, signed with the carrier's token."""
    headers = {"X-Twilio-Signature": sign(carrier.auth_token, url, params)}
    return await http.post(url, data=params, headers=headers)


def mulaw_tone(ms: int) -> list[bytes]:
    """Caller "speech": 20ms mulaw frames of a tone the stub STT hears."""
    ulaw = audioop.lin2ulaw(tone(ms, SAMPLE_RATE), 2)
    return [ulaw[i : i + FRAME_BYTES] for i in range(0, len(ulaw), FRAME_BYTES)]


@dataclass
class TwilioCall:
    """One live inbound call: the media stream, from the caller's side."""

    carrier: FakeCarrier
    http: httpx.AsyncClient
    call_sid: str
    from_number: str
    to_number: str
    call_id: str
    stream_sid: str
    twiml: str
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
        """The caller hangs up: `stop`, close the stream, report `completed`."""
        if not self.closed.is_set():
            await self._send({"event": "stop", "stop": {"callSid": self.call_sid}})
            await self._ws.close()
        await self.finish()

    async def finish(self, status: str = "completed") -> None:
        """What Twilio does once the stream is gone: stop the line and post the
        status callback to the number's configured URL."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        number = self.carrier.number(self.to_number)
        if number.status_callback_url:
            await post_signed(
                self.http,
                self.carrier,
                number.status_callback_url,
                self._params(CallStatus=status, CallDuration="1"),
            )

    def _params(self, **extra: str) -> dict[str, str]:
        return {
            "AccountSid": self.carrier.account_sid,
            "ApiVersion": "2010-04-01",
            "CallSid": self.call_sid,
            "From": self.from_number,
            "To": self.to_number,
            "Direction": "inbound",
            **extra,
        }

    async def _send(self, message: dict) -> None:
        await self._ws.send(json.dumps({**message, "streamSid": self.stream_sid}))

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
                {
                    "event": "media",
                    "media": {
                        "track": "inbound",
                        "chunk": str(chunk),
                        "timestamp": str(chunk * FRAME_MS),
                        "payload": base64.b64encode(payload).decode(),
                    },
                }
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


async def place_inbound_call(
    carrier: FakeCarrier,
    http: httpx.AsyncClient,
    *,
    from_number: str,
    to_number: str,
) -> TwilioCall:
    """Ring `to_number`: POST its configured voice URL, then open the stream
    the TwiML asks for. The number must have been configured by the app
    (binding it through the API does that)."""
    voice_url = carrier.number(to_number).voice_url
    if not voice_url:
        raise AssertionError(f"{to_number} has no voice URL; bind it through the API")
    call_sid = "CA" + secrets.token_hex(16)
    params = {
        "AccountSid": carrier.account_sid,
        "ApiVersion": "2010-04-01",
        "CallSid": call_sid,
        "CallStatus": "ringing",
        "Direction": "inbound",
        "From": from_number,
        "To": to_number,
    }
    response = await post_signed(http, carrier, voice_url, params)
    response.raise_for_status()
    stream = ET.fromstring(response.text).find("./Connect/Stream")
    if stream is None:
        raise AssertionError(f"TwiML has no <Connect><Stream>: {response.text}")
    custom = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    stream_sid = "MZ" + secrets.token_hex(16)

    ws = await websockets.connect(stream.get("url"))
    call = TwilioCall(
        carrier=carrier,
        http=http,
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        call_id=custom.get("callId", ""),
        stream_sid=stream_sid,
        twiml=response.text,
        _ws=ws,
    )
    await ws.send(
        json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    )
    await call._send(
        {
            "event": "start",
            "start": {
                "accountSid": carrier.account_sid,
                "streamSid": stream_sid,
                "callSid": call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": SAMPLE_RATE,
                    "channels": 1,
                },
                "customParameters": custom,
            },
        }
    )
    call._tasks = [
        asyncio.create_task(call._send_media()),
        asyncio.create_task(call._receive()),
    ]
    return call
