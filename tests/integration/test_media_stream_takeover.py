"""A carrier media stream must prove it is the call's (the media-stream takeover).

Every carrier's media route used to trust the call id it was handed — a TwiML
`<Parameter>` on Twilio, a URL segment on Telnyx, a channel id on Asterisk —
and the shared entry point checked only that such a call existed. A call id
is not a secret (it rides in webhooks, call-init, the API, logs), so anyone
holding one could open a socket on any carrier's route and have the real
pipeline built on it: the caller's agent fed their audio, its replies — and
its tools — theirs.

Now the entry point (`webhooks/media_stream.run_media_stream`) refuses, before
anything is built:
- a stream without the call's media token (an HMAC of the call id, handed only
  to the carrier in that call's own stream setup);
- a stream on another carrier's route than the call's;
- a second stream for a call that already has one.

Each case is driven through the real app (Seam A) next to a live call on the
real carrier flow, which must carry on unharmed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import xml.etree.ElementTree as ET
from typing import Any

import pytest
import websockets

from tests.harness import HARNESS_AGENT, Harness, Script, run_app, telnyx, twilio

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
AGENT = {**HARNESS_AGENT, "first_message": "Hello there."}


def _count_pipelines(monkeypatch: Any) -> list[str]:
    """The call ids a pipeline was built for, in order."""
    from turncall.webhooks import media_stream

    built: list[str] = []
    real = media_stream.build_call_pipeline

    async def counting(*args: Any, **kwargs: Any) -> Any:
        built.append(str(kwargs["call_context"].call_id))
        return await real(*args, **kwargs)

    monkeypatch.setattr(media_stream, "build_call_pipeline", counting)
    return built


async def _refused(ws: Any) -> bool:
    """Whether the app closed the socket without ever speaking on it."""
    try:
        while True:
            message = await asyncio.wait_for(ws.recv(), 3)
            if isinstance(message, str) and '"media"' in message:
                return False
    except websockets.ConnectionClosed:
        return True
    except TimeoutError:
        return False


async def _twilio_attempt(app: Harness, params: dict[str, str]) -> bool:
    url = app.base_url.replace("http", "ws") + "/ws/media-stream"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        start = {
            "streamSid": "MZforged",
            "callSid": "CAforged",
            "customParameters": params,
        }
        await ws.send(json.dumps({"event": "start", "start": start}))
        return await _refused(ws)


async def _asterisk_attempt(app: Harness, channel_id: str) -> bool:
    url = app.base_url.replace("http", "ws") + "/ws/asterisk-media"
    async with websockets.connect(url, subprotocols=["media"]) as ws:
        start = {"event": "MEDIA_START", "channel_id": channel_id, "format": "ulaw"}
        await ws.send(json.dumps(start))
        return await _refused(ws)


async def _telnyx_attempt(url: str, call_control_id: str) -> bool:
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "connected", "version": "1.0.0"}))
            start = {
                "call_control_id": call_control_id,
                "media_format": {"encoding": "PCMU", "sample_rate": 8000},
            }
            await ws.send(
                json.dumps({"event": "start", "stream_id": "S1", "start": start})
            )
            return await _refused(ws)
    except websockets.InvalidStatus:
        return True  # no such route: refused at the handshake


def _token_of(twiml: str) -> str:
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    return params.get("mediaToken", "")


async def test_twilio_stream_must_carry_the_calls_token(monkeypatch) -> None:
    built = _count_pipelines(monkeypatch)
    async with run_app(
        Script(agent=("Hi.",)), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await twilio.place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        token = _token_of(call.twiml)

        no_token = await _twilio_attempt(app, {"callId": call.call_id})
        wrong = await _twilio_attempt(
            app, {"callId": call.call_id, "mediaToken": "0" * 64}
        )
        # The real token, leaked: still one stream per call.
        second = await _twilio_attempt(
            app, {"callId": call.call_id, "mediaToken": token}
        )
        still_up = not call.closed.is_set()
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    assert token, "the TwiML hands Twilio no media token"
    assert (no_token, wrong, second) == (True, True, True)
    assert still_up
    assert built == [call.call_id]


async def test_a_twilio_call_cannot_be_opened_on_another_carriers_route(
    monkeypatch,
) -> None:
    """Even with its token, a Twilio call's media does not come in on the
    Asterisk route: the route's carrier must be the call's."""
    from turncall.services.media_token import media_token

    built = _count_pipelines(monkeypatch)
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call_id = await _ring_twilio_without_stream(app, number)
        token = media_token(uuid.UUID(call_id))
        refused = await _asterisk_attempt(app, f"turncall-media-{call_id}-{token}")
        status = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]["status"]

    assert refused
    assert built == []
    assert status == "initiated"


async def _ring_twilio_without_stream(app: Harness, number: str) -> str:
    """The inbound webhook only: a call row, its TwiML, and no stream yet."""
    params = {
        "AccountSid": app.carrier.account_sid,
        "ApiVersion": "2010-04-01",
        "CallSid": "CA" + uuid.uuid4().hex,
        "CallStatus": "ringing",
        "Direction": "inbound",
        "From": CALLER,
        "To": number,
    }
    response = await twilio.post_signed(
        app.api, app.carrier, app.carrier.number(number).voice_url, params
    )
    response.raise_for_status()
    stream = ET.fromstring(response.text).find("./Connect/Stream")
    return {p.get("name"): p.get("value") for p in stream.findall("Parameter")}[
        "callId"
    ]


async def test_telnyx_stream_must_carry_the_calls_token(monkeypatch) -> None:
    built = _count_pipelines(monkeypatch)
    async with run_app(
        Script(agent=("Hi.",)), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        call = await telnyx.place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        stream_url = app.carrier.actions_of("answer")[0].params["stream_url"]
        base = app.base_url.replace("http", "ws") + "/ws/telnyx/media-stream"

        # The call_control_id is the real one throughout: a forger who has it
        # still needs the token.
        bare = await _telnyx_attempt(f"{base}/{call.call_id}", call.call_sid)
        wrong = await _telnyx_attempt(
            f"{base}/{call.call_id}/{'0' * 64}", call.call_sid
        )
        second = await _telnyx_attempt(stream_url, call.call_sid)
        still_up = not call.closed.is_set()
        hang_ups = list(app.carrier.actions_of("hang_up"))
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    assert (bare, wrong, second) == (True, True, True)
    assert still_up
    assert hang_ups == [], "a refused stream hung the real call up"
    assert built == [call.call_id]


async def test_asterisk_stream_must_carry_the_calls_token(monkeypatch) -> None:
    built = _count_pipelines(monkeypatch)
    async with run_app(
        Script(agent=("Hi.",)), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_asterisk_number(await app.create_agent(AGENT))
        call = await app.ring_asterisk(CALLER, number)
        await call.wait_for_agent_speech(1)

        bare = await _asterisk_attempt(app, f"turncall-media-{call.call_id}")
        wrong = await _asterisk_attempt(
            app, f"turncall-media-{call.call_id}-{'0' * 64}"
        )
        second = await _asterisk_attempt(app, call.media_id)
        still_up = not call.closed.is_set()
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    assert call.media_id != f"turncall-media-{call.call_id}", "no token was minted"
    assert (bare, wrong, second) == (True, True, True)
    assert still_up
    assert built == [call.call_id]
