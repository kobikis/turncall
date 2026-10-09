"""Telnyx's inbound edge through the running app (#217, ADR-0022).

The happy path is the carrier contract's (`test_carrier_contract.py`, which
holds Telnyx to Twilio's outcome); this is what only Telnyx's edge decides:
whose Ed25519 key verifies a webhook, what a missing key does to a call, a
caller who hangs up before the stream opens, and a stream that is not the
call's. Driven through Seam A — the real app, stub providers, the fake carrier
recording Telnyx's commands.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.harness import HARNESS_AGENT, Harness, Script, run_app, telnyx

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
AGENT = {**HARNESS_AGENT, "first_message": "Hello from Telnyx."}
# Fixed so a credential a crashed run left behind stays readable by the next.
ENCRYPTION_KEY = "S5vUzUzp1l-3r5gVsE1lNwUyliWKaHq4rZNT3xrsE6c="


def _new_id() -> str:
    """A fresh `call_control_id`: a call is looked up by it."""
    return f"v3:{uuid.uuid4().hex}"


def _initiated(call_control_id: str, to_number: str) -> dict[str, Any]:
    return telnyx.event(
        "call.initiated",
        {
            "call_control_id": call_control_id,
            "call_session_id": "session-1",
            "from": CALLER,
            "to": to_number,
            "direction": "incoming",
            "state": "parked",
        },
    )


def _hangup(call_control_id: str, to_number: str) -> dict[str, Any]:
    return telnyx.event(
        "call.hangup",
        {
            "call_control_id": call_control_id,
            "from": CALLER,
            "to": to_number,
            "hangup_cause": "normal_clearing",
            "hangup_source": "caller",
        },
    )


def _answered(app: Harness, call_control_id: str) -> list[Any]:
    return [
        a
        for a in app.carrier.actions_of("answer")
        if a.params["call_sid"] == call_control_id
    ]


async def _events(app: Harness, call_id: str) -> list[dict[str, Any]]:
    response = await app.api.get(f"/v1/calls/{call_id}/events")
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(public).decode()


async def test_unsigned_and_wrongly_signed_webhooks_are_refused(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        body = _initiated("v3:forged", number)
        unsigned = await app.api.post(
            telnyx.WEBHOOK_PATH,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        someone_else = await telnyx.post_signed(
            app.api, app.carrier, body, signing_key=Ed25519PrivateKey.generate()
        )
        replayed = await telnyx.post_signed(
            app.api, app.carrier, body, timestamp=str(int(time.time()) - 600)
        )
        tampered_raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        signature = telnyx.sign(app.carrier.telnyx_signing_key, ts, tampered_raw)
        tampered = await app.api.post(
            telnyx.WEBHOOK_PATH,
            content=tampered_raw.replace(CALLER.encode(), b"+15559999999"),
            headers={
                "content-type": "application/json",
                "telnyx-signature-ed25519": signature,
                "telnyx-timestamp": ts,
            },
        )
        calls = (await app.api.get("/v1/calls")).json()

    assert [r.status_code for r in (unsigned, someone_else, replayed, tampered)] == [
        403,
        403,
        403,
        403,
    ]
    assert app.carrier.actions_of("answer") == []
    assert calls["total"] == 0


async def test_a_missing_telnyx_key_is_reported(monkeypatch) -> None:
    """No key at any level: the call cannot be answered, and says which key."""
    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        from turncall.config.settings import get_settings

        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        monkeypatch.setenv("TELNYX_API_KEY", "")
        get_settings.cache_clear()
        response = await telnyx.post_signed(
            app.api, app.carrier, _initiated(cc, number)
        )
        ended = await app.sink.wait_for("call.ended")
        events = await _events(app, ended["call_id"])

    assert response.status_code == 200
    assert app.carrier.actions_of("answer") == []
    assert ended["payload"]["status"] == "failed"
    assert ended["payload"]["ended_reason"] == "pipeline_error"
    errors = [e["payload"] for e in events if e["event_type"] == "error.raised"]
    assert [e["missing_keys"] for e in errors] == [["telnyx"]]


async def test_a_caller_who_hangs_up_before_the_stream_opens(monkeypatch) -> None:
    """No pipeline ever runs, so `call.hangup` is what ends the call."""
    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        await telnyx.post_signed(app.api, app.carrier, _initiated(cc, number))
        assert _answered(app, cc)
        await telnyx.post_signed(app.api, app.carrier, _hangup(cc, number))
        ended = (await app.sink.wait_for("call.ended"))["payload"]

    assert ended["status"] == "completed"
    assert ended["ended_reason"] == "customer_ended_call"
    assert ended["provider_call_sid"] == cc


async def test_a_stream_that_is_not_the_calls_is_refused(monkeypatch) -> None:
    """A call id in a URL is a guess away from someone else's call: the stream
    must name the call's own `call_control_id`, or it gets no pipeline — and
    does not get the call hung up either."""
    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        await telnyx.post_signed(app.api, app.carrier, _initiated(cc, number))
        stream_url = _answered(app, cc)[0].params["stream_url"]
        async with websockets.connect(stream_url) as ws:
            await ws.send(json.dumps({"event": "connected", "version": "1.0.0"}))
            start = {
                "call_control_id": "v3:forged",
                "media_format": {"encoding": "PCMU", "sample_rate": 8000},
            }
            await ws.send(
                json.dumps({"event": "start", "stream_id": "S1", "start": start})
            )
            with pytest.raises(websockets.ConnectionClosed):
                await ws.recv()
        call_id = stream_url.rsplit("/", 2)[-2]
        status = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]["status"]

    assert app.carrier.actions_of("hang_up") == []
    assert status == "initiated"  # no pipeline ran


async def test_a_project_credential_replaces_the_platform_account(monkeypatch) -> None:
    """The project's own Telnyx account sends its numbers' webhooks, so its
    public key verifies them and its API key answers (ADR-0020)."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", ENCRYPTION_KEY)
    own_signing_key, own_public_key = _keypair()
    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        stored = await app.api.put(
            "/v1/credentials/telnyx",
            json={"api_key": "KEY-project", "public_key": own_public_key},
        )
        assert stored.status_code == 200, stored.text
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        by_platform = await telnyx.post_signed(
            app.api, app.carrier, _initiated(cc, number)
        )
        by_project = await telnyx.post_signed(
            app.api,
            app.carrier,
            _initiated(cc, number),
            signing_key=own_signing_key,
        )
        answers = _answered(app, cc)
        await telnyx.post_signed(
            app.api,
            app.carrier,
            _hangup(cc, number),
            signing_key=own_signing_key,
        )
        await app.sink.wait_for("call.ended")

    assert by_platform.status_code == 403
    assert by_project.status_code == 200
    assert [a.account_sid for a in answers] == ["KEY-project"]


async def test_a_retried_call_initiated_opens_one_call(monkeypatch) -> None:
    """Telnyx retries a webhook it has no 200 for. A retry that lands while
    the first is still resolving the agent (a slow call-init) finds no row
    yet, and used to open the call a second time: a second slot, a second
    row, a second `answer` for the same leg."""
    import asyncio

    from turncall.services import inbound_call

    resolve = inbound_call._resolve_agent

    async def slow_resolve(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        return await resolve(*args, **kwargs)

    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        monkeypatch.setattr(inbound_call, "_resolve_agent", slow_resolve)
        body = _initiated(cc, number)
        first, retry = await asyncio.gather(
            telnyx.post_signed(app.api, app.carrier, body),
            telnyx.post_signed(app.api, app.carrier, body),
        )
        calls = (await app.api.get("/v1/calls")).json()

    assert (first.status_code, retry.status_code) == (200, 200)
    assert len(_answered(app, cc)) == 1
    assert calls["total"] == 1


async def test_an_answer_that_fails_hangs_the_leg_up(monkeypatch) -> None:
    """An `answer` Telnyx applied but whose reply never came back (a timeout
    on our side) left the caller on a live, silent leg: the call was failed
    and its slot freed, but nothing told Telnyx to end it."""
    from tests.harness.carrier import FakeTelnyxAdapter

    async def answered_then_lost(self: Any, call_control_id: str, **kw: Any) -> None:
        await self._record("answer", call_sid=call_control_id, **kw)
        raise TimeoutError("no reply from Telnyx")

    cc = _new_id()
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        monkeypatch.setattr(FakeTelnyxAdapter, "answer_call", answered_then_lost)
        response = await telnyx.post_signed(
            app.api, app.carrier, _initiated(cc, number)
        )
        ended = (await app.sink.wait_for("call.ended"))["payload"]

    assert response.status_code == 200
    assert ended["status"] == "failed"
    hang_ups = [
        a for a in app.carrier.actions_of("hang_up") if a.params["call_sid"] == cc
    ]
    assert len(hang_ups) == 1


async def test_a_failed_telnyx_transfer_leaves_the_ended_call_ended(
    monkeypatch,
) -> None:
    """A Telnyx transfer stops the agent's stream first; when a later step
    fails, the caller is hung up and the call finalizes while call control is
    still waiting on the failing command, which then puts the record back to
    `in_progress`. That revert must not land over the finalize. It does not:
    the transfer's `transferring` write holds the call row until its session
    commits, revert included, so the finalize's write queues behind it and
    wins. This pins that; a commit added mid-transfer would break it."""
    import asyncio

    import turncall.storage.redis as redis_module
    from tests.harness import ToolCall
    from tests.harness.carrier import FakeTelnyxAdapter
    from tests.integration.test_carrier_contract import _MemoryRedis

    monkeypatch.setattr(redis_module, "get_redis", lambda: _MemoryRedis())

    async def slow_failure(self: Any, call_control_id: str, text: str, **kw: Any):
        await self._record("speak", call_sid=call_control_id, text=text, **kw)
        await asyncio.sleep(1.5)
        raise TimeoutError("Telnyx did not answer the speak command")

    transfer = {"target_number": "+15559990000", "transfer_message": "One moment."}
    script = Script(
        caller=("Put me through.",),
        agent=(ToolCall("transfer_call", transfer), "Sorry, that did not work."),
    )
    agent = {
        **AGENT,
        "tools": [
            {
                "name": "transfer_call",
                "description": "transfer",
                "parameters_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        monkeypatch.setattr(FakeTelnyxAdapter, "speak", slow_failure)
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(agent)
        )
        call = await telnyx.place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await app.carrier.wait_for("stop_streaming", timeout=10)
        await call.close_stream()  # Telnyx stops the stream it was told to
        await app.sink.wait_for("call.ended")
        await asyncio.sleep(2.0)  # the slow speak fails, the caller is hung up
        status = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    assert status["status"] == "completed", status
