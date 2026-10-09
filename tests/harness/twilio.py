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

from tests.harness.carrier import CarrierAction, FakeCarrier
from tests.harness.media_call import FRAME_MS, SAMPLE_RATE, CarrierCall, TransferHeard
from turncall.adapters.telephony.twilio_adapter import TwilioAdapter


def _said(twiml: str) -> list[str]:
    """The `<Say>` lines of a TwiML document, in order."""
    return [say.text or "" for say in ET.fromstring(twiml).findall("Say")]


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
    # Where `finish` reports the ending: the dial's own callback for an
    # outbound call, else the called number's configured one.
    status_callback_url: str = ""
    direction: str = "inbound"

    def _stop_message(self) -> dict[str, Any]:
        return {"event": "stop", "stop": {"callSid": self.call_sid}}

    async def complete_transfer(self, *, answered_by: str | None) -> TransferHeard:
        """What Twilio does with the transfer TwiML the app swapped in
        (ADR-0009): the stream ends at the swap, `<Say>` plays to the caller,
        `<Dial>` rings the operator with ringback to the caller; on answer the
        operator hears the `<Number url>` whisper, then the legs bridge and the
        AMD callback reports in parallel; on no answer the `<Dial action>`
        result decides what the caller hears last. Ends the call."""
        transfer = await self.carrier.wait_for("transfer", timeout=10)
        await self.close_stream()
        params = {k: v for k, v in transfer.params.items() if k != "call_sid"}
        twiml = ET.fromstring(TwilioAdapter.build_transfer_twiml(**params))
        dial = twiml.find("Dial")
        number = dial.find("Number") if dial is not None else None
        heard = TransferHeard(
            target=transfer.params["target_number"],
            caller=[say.text or "" for say in twiml.findall("Say")],
            ringing=dial is not None,
        )
        leg = self._params(CallSid="CA" + secrets.token_hex(16))
        if answered_by is not None and dial is not None:
            if number is not None and number.get("url"):
                whisper = await post_signed(
                    self.http, self.carrier, number.get("url", ""), leg
                )
                heard.operator = _said(whisper.text)
            heard.bridged = True
            if number is not None and number.get("amdStatusCallback"):
                amd = {"human": "human", "machine": "machine_start"}[answered_by]
                await post_signed(
                    self.http,
                    self.carrier,
                    number.get("amdStatusCallback", ""),
                    {**leg, "AnsweredBy": amd},
                )
        elif dial is not None and dial.get("action"):
            result = await post_signed(
                self.http,
                self.carrier,
                dial.get("action", ""),
                self._params(DialCallStatus="no-answer"),
            )
            heard.caller += _said(result.text)
        await self.finish()
        return heard

    async def finish(self, status: str = "completed") -> None:
        """What Twilio does once the stream is gone: stop the line and post the
        status callback to the number's configured URL."""
        await self._stop_streaming()
        url = (
            self.status_callback_url
            or self.carrier.number(self.to_number).status_callback_url
        )
        if url:
            await post_signed(
                self.http,
                self.carrier,
                url,
                self._params(CallStatus=status, CallDuration="1"),
            )

    def _params(self, **extra: str) -> dict[str, str]:
        return {
            "AccountSid": self.carrier.account_sid,
            "ApiVersion": "2010-04-01",
            "CallSid": self.call_sid,
            "From": self.from_number,
            "To": self.to_number,
            "Direction": self.direction,
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
    call_type: type[TwilioCall] = TwilioCall,
) -> TwilioCall:
    """Ring `to_number`: POST its configured voice URL, then open the stream
    the TwiML asks for. The number must have been configured by the app
    (binding it through the API does that). `call_type` is the caller to hand
    the stream to — a `TwilioCall` subclass that measures, for #200's load
    generator."""
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
    return await _connect(carrier, http, voice_url, params, call_type=call_type)


async def answer_outbound_call(
    carrier: FakeCarrier, http: httpx.AsyncClient, dial: CarrierAction
) -> TwilioCall:
    """The callee picks up a call TurnCall dialled: POST the dial's webhook
    URL, then open the stream its TwiML asks for. `finish` reports to the
    dial's status callback."""
    params = {
        "AccountSid": carrier.account_sid,
        "ApiVersion": "2010-04-01",
        "CallSid": dial.params["call_sid"],
        "CallStatus": "in-progress",
        "Direction": "outbound-api",
        "From": dial.params["from_number"],
        "To": dial.params["to_number"],
    }
    return await _connect(
        carrier,
        http,
        dial.params["webhook_url"],
        params,
        status_callback_url=dial.params["status_callback_url"] or "",
    )


async def _connect(
    carrier: FakeCarrier,
    http: httpx.AsyncClient,
    voice_url: str,
    params: dict[str, str],
    *,
    status_callback_url: str = "",
    call_type: type[TwilioCall] = TwilioCall,
) -> TwilioCall:
    """POST the voice URL, then open the `<Stream>` and start the media."""
    call_sid, from_number, to_number = params["CallSid"], params["From"], params["To"]
    response = await post_signed(http, carrier, voice_url, params)
    response.raise_for_status()
    stream = ET.fromstring(response.text).find("./Connect/Stream")
    if stream is None:
        raise AssertionError(f"TwiML has no <Connect><Stream>: {response.text}")
    custom = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    stream_sid = "MZ" + secrets.token_hex(16)

    ws = await websockets.connect(stream.get("url"))
    call = call_type(
        carrier=carrier,
        http=http,
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        call_id=custom.get("callId", ""),
        stream_sid=stream_sid,
        twiml=response.text,
        _ws=ws,
        status_callback_url=status_callback_url,
        direction=params["Direction"],
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
