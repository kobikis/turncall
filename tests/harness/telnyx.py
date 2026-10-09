"""Telnyx's side of a call: signed Call Control webhooks and a media stream.

What Telnyx does on an inbound call, in order: POST `call.initiated` to the
number's Call Control application (JSON, Ed25519-signed over
`{timestamp}|{body}`), wait for the `answer` command, then open the
`stream_url` that command named and send `connected`, `start` and one 20ms
PCMU `media` frame every 20ms — silence included — and `stop` when the call
ends. Closing the stream does not end a Telnyx call; a `hangup` command does,
and then `call.hangup` is posted. `TelnyxCall` does exactly that at real-time
pace. An outbound call (#218) has no answer step: the dial names the stream,
and `answer_outbound_call` opens it once the callee "picks up". Field names follow Telnyx's media streaming and Call Control docs and
pipecat's `TelnyxFrameSerializer`.

Until #219 configures a number's Call Control application on bind, the
webhook URL is the one an operator sets by hand: `/webhooks/telnyx/voice`.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.harness.carrier import CarrierAction, FakeCarrier
from tests.harness.media_call import FRAME_MS, SAMPLE_RATE, CarrierCall

WEBHOOK_PATH = "/webhooks/telnyx/voice"


def sign(signing_key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    """`telnyx-signature-ed25519` for `body` signed at `timestamp`."""
    return base64.b64encode(signing_key.sign(timestamp.encode() + b"|" + body)).decode()


def event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A Call Control webhook body."""
    return {
        "data": {
            "record_type": "event",
            "event_type": event_type,
            "id": str(uuid.uuid4()),
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        },
        "meta": {"attempt": 1, "delivered_to": WEBHOOK_PATH},
    }


async def post_signed(
    http: httpx.AsyncClient,
    carrier: FakeCarrier,
    body: dict[str, Any],
    *,
    signing_key: Ed25519PrivateKey | None = None,
    timestamp: str | None = None,
    url: str = WEBHOOK_PATH,
) -> httpx.Response:
    """POST a webhook the way Telnyx does, signed with the account's key
    (or `signing_key`, to sign as somebody else), to `url`."""
    raw = json.dumps(body).encode()
    signed_at = timestamp or str(int(time.time()))
    key = signing_key or carrier.telnyx_signing_key
    headers = {
        "content-type": "application/json",
        "telnyx-signature-ed25519": sign(key, signed_at, raw),
        "telnyx-timestamp": signed_at,
    }
    return await http.post(url, content=raw, headers=headers)


async def bind_number(
    api: httpx.AsyncClient, carrier: FakeCarrier, agent_id: str
) -> str:
    """Own a fresh number on Telnyx and bind it to the agent through the API."""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = carrier.add_number(e164)
    response = await api.post(
        "/v1/phone-numbers",
        json={
            "carrier": "telnyx",
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
        },
    )
    assert response.status_code == 201, response.text
    return e164


@dataclass(kw_only=True)
class TelnyxCall(CarrierCall):
    """One live Telnyx call: its media stream, from the far end's side.
    `call_sid` is the call's `call_control_id`. An outgoing one (TurnCall
    dialled it) reports to the dial's own `webhook_url`."""

    stream_id: str
    call_session_id: str
    direction: str = "incoming"
    webhook_url: str = WEBHOOK_PATH
    _sequence: int = 0

    async def hang_up(self) -> None:
        """The caller hangs up: `stop`, close the stream, post `call.hangup`."""
        if not self.closed.is_set():
            await self._send(
                {
                    "event": "stop",
                    "stop": {"user_id": "harness", "call_control_id": self.call_sid},
                }
            )
            await self._ws.close()
        await self.finish(source="caller")

    async def finish(self, status: str = "completed", source: str = "callee") -> None:
        """What Telnyx does once the call is over: stop the line and post
        `call.hangup` to the Call Control application."""
        await self._stop_streaming()
        await post_signed(
            self.http,
            self.carrier,
            event(
                "call.hangup",
                self.payload(
                    hangup_cause="normal_clearing", hangup_source=source, state="hangup"
                ),
            ),
            url=self.webhook_url,
        )

    def payload(self, **extra: Any) -> dict[str, Any]:
        """The call's fields, as every Call Control webhook carries them."""
        return {
            "call_control_id": self.call_sid,
            "call_leg_id": self.call_sid.removeprefix("v3:"),
            "call_session_id": self.call_session_id,
            "connection_id": "harness-connection",
            "from": self.from_number,
            "to": self.to_number,
            "direction": self.direction,
            **extra,
        }

    async def _send(self, message: dict[str, Any]) -> None:
        self._sequence += 1
        await self._ws.send(
            json.dumps(
                {
                    **message,
                    "sequence_number": str(self._sequence),
                    "stream_id": self.stream_id,
                }
            )
        )

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
) -> TelnyxCall:
    """Ring `to_number`: post `call.initiated`, and open the media stream the
    app's `answer` command asked for."""
    call_control_id = "v3:" + secrets.token_urlsafe(24)
    call_session_id = str(uuid.uuid4())
    initiated = {
        "call_control_id": call_control_id,
        "call_leg_id": call_control_id.removeprefix("v3:"),
        "call_session_id": call_session_id,
        "connection_id": "harness-connection",
        "from": from_number,
        "to": to_number,
        "direction": "incoming",
        "state": "parked",
        "start_time": datetime.now(UTC).isoformat(),
    }
    response = await post_signed(http, carrier, event("call.initiated", initiated))
    response.raise_for_status()
    # The app answers within the webhook it is handed.
    answers = [
        a
        for a in carrier.actions_of("answer")
        if a.params["call_sid"] == call_control_id
    ]
    if not answers:
        raise AssertionError(f"the app did not answer {call_control_id}")
    stream_url = answers[0].params["stream_url"]
    await post_signed(
        http, carrier, event("call.answered", {**initiated, "state": "answered"})
    )

    return await _open_stream(
        carrier,
        http,
        initiated,
        stream_url=stream_url,
        webhook_url=WEBHOOK_PATH,
    )


async def answer_outbound_call(
    carrier: FakeCarrier, http: httpx.AsyncClient, dial: CarrierAction
) -> TelnyxCall:
    """The callee picks up a call TurnCall dialled on Telnyx: post
    `call.initiated` and `call.answered` to the dial's `webhook_url`, then
    open the `stream_url` the dial named — no answer command on this side."""
    initiated = {
        "call_control_id": dial.params["call_sid"],
        "call_leg_id": dial.params["call_sid"].removeprefix("v3:"),
        "call_session_id": str(uuid.uuid4()),
        "connection_id": dial.params["connection_id"],
        "from": dial.params["from_number"],
        "to": dial.params["to_number"],
        "direction": "outgoing",
        "state": "bridging",
        "start_time": datetime.now(UTC).isoformat(),
    }
    url = dial.params["webhook_url"]
    for event_type, state in (
        ("call.initiated", "bridging"),
        ("call.answered", "answered"),
    ):
        response = await post_signed(
            http, carrier, event(event_type, {**initiated, "state": state}), url=url
        )
        response.raise_for_status()
    return await _open_stream(
        carrier, http, initiated, stream_url=dial.params["stream_url"], webhook_url=url
    )


async def not_answered(
    carrier: FakeCarrier,
    http: httpx.AsyncClient,
    dial: CarrierAction,
    *,
    cause: str = "timeout",
) -> None:
    """Nobody picks up a call TurnCall dialled: Telnyx posts `call.hangup`
    with why (`timeout`, `user_busy`, ...)."""
    payload = {
        "call_control_id": dial.params["call_sid"],
        "connection_id": dial.params["connection_id"],
        "from": dial.params["from_number"],
        "to": dial.params["to_number"],
        "direction": "outgoing",
        "state": "hangup",
        "hangup_cause": cause,
        "hangup_source": "callee",
    }
    response = await post_signed(
        http, carrier, event("call.hangup", payload), url=dial.params["webhook_url"]
    )
    response.raise_for_status()


async def _open_stream(
    carrier: FakeCarrier,
    http: httpx.AsyncClient,
    call: dict[str, Any],
    *,
    stream_url: str,
    webhook_url: str,
) -> TelnyxCall:
    """Open the media stream and send `connected`, `start`, then media."""
    call_control_id = call["call_control_id"]
    ws = await websockets.connect(stream_url)
    live = TelnyxCall(
        carrier=carrier,
        http=http,
        call_sid=call_control_id,
        from_number=call["from"],
        to_number=call["to"],
        # The app names the call in the stream URL it gave Telnyx.
        call_id=stream_url.rstrip("/").rsplit("/", 1)[-1],
        stream_id=str(uuid.uuid4()).upper(),
        call_session_id=call["call_session_id"],
        direction=call["direction"],
        webhook_url=webhook_url,
        _ws=ws,
    )
    await ws.send(json.dumps({"event": "connected", "version": "1.0.0"}))
    await live._send(
        {
            "event": "start",
            "start": {
                "user_id": "harness",
                "call_control_id": call_control_id,
                "call_session_id": call["call_session_id"],
                "from": call["from"],
                "to": call["to"],
                "tags": [],
                "client_state": None,
                "media_format": {
                    "encoding": "PCMU",
                    "sample_rate": SAMPLE_RATE,
                    "channels": 1,
                },
            },
        }
    )
    live._start_streaming()
    return live
