"""Twilio's side of a call: signed webhooks and a Media Streams client.

What Twilio does on an inbound call, in order: POST the number's voice URL
(signed), read the TwiML, open the `<Stream>` WebSocket, send `connected` and
`start`, then one 20ms mulaw `media` frame every 20ms for as long as the call
lasts — silence included — and finally `stop`. When the stream ends the call
ends, and the number's status callback hears `completed`. `TwilioCall` does
exactly that at real-time pace.

Telnyx's sibling is `telnyx.py`; both calls share `media_call.CarrierCall`
(`say`, `wait_for_agent_speech`, `hang_up`, `closed`), so a contract test can
run one script against each.
"""

from __future__ import annotations

import json
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from twilio.request_validator import RequestValidator

from tests.harness.carrier import FakeCarrier
from tests.harness.media_call import FRAME_MS, SAMPLE_RATE, CarrierCall


def sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    """`X-Twilio-Signature` for a POST of `params` to `url`."""
    return RequestValidator(auth_token).compute_signature(url, params)


async def post_signed(
    http: httpx.AsyncClient, carrier: FakeCarrier, url: str, params: dict[str, str]
) -> httpx.Response:
    """POST a form the way Twilio does, signed with the carrier's token."""
    headers = {"X-Twilio-Signature": sign(carrier.auth_token, url, params)}
    return await http.post(url, data=params, headers=headers)


@dataclass(kw_only=True)
class TwilioCall(CarrierCall):
    """One live inbound Twilio call: its Media Stream, from the caller's side."""

    stream_sid: str
    twiml: str

    async def hang_up(self) -> None:
        """The caller hangs up: `stop`, close the stream, report `completed`."""
        if not self.closed.is_set():
            await self._send({"event": "stop", "stop": {"callSid": self.call_sid}})
            await self._ws.close()
        await self.finish()

    async def finish(self, status: str = "completed") -> None:
        """What Twilio does once the stream is gone: stop the line and post the
        status callback to the number's configured URL."""
        await self._stop_streaming()
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

    async def _send(self, message: dict[str, Any]) -> None:
        await self._ws.send(json.dumps({**message, "streamSid": self.stream_sid}))

    def _media_message(self, chunk: int, payload: str) -> dict[str, Any]:
        return {
            "event": "media",
            "media": {
                "track": "inbound",
                "chunk": str(chunk),
                "timestamp": str(chunk * FRAME_MS),
                "payload": payload,
            },
        }


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
    call._start_streaming()
    return call
