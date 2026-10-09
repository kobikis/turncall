"""`POST /v1/calls/{id}/messages` reaches the live call, or says it did not (#215).

Over the running app: an inbound Twilio call, a message sent through the API
while it is live, and the agent's next reply decided by what its model was
actually given — so the reply the caller hears is the proof the message landed
in the context. Then the two ways the endpoint must refuse to claim success:
the call is gone (409), and the call is there but does not acknowledge (504).
"""

import asyncio
import os

import pytest
from redis.asyncio import Redis

from tests.harness import HARNESS_AGENT, Script, place_inbound_call, run_app
from turncall.services.live_call_channel import ControlRefused

pytestmark = pytest.mark.integration

NOTE = "The caller is a VIP customer."


def _answer_from_context(messages: list[dict]) -> str:
    seen = any(m.get("role") == "system" and m.get("content") == NOTE for m in messages)
    return "Welcome back, valued customer." if seen else "Hello, who is this?"


@pytest.fixture
async def redis_up():
    """These tests need the control channel's Redis; CI runs one."""
    client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable ({type(exc).__name__})")
    finally:
        await client.aclose()


async def _live_call(app):
    number = await app.bind_number(
        await app.create_agent({**HARNESS_AGENT, "first_message": "Hello."})
    )
    call = await place_inbound_call(
        app.carrier, app.api, from_number="+15550001234", to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting: the call is running
    return call


async def test_a_message_reaches_the_live_calls_model(monkeypatch, redis_up):
    script = Script(caller=("Hi there.",), agent=(_answer_from_context,))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _live_call(app)

        sent = await app.api.post(
            f"/v1/calls/{call.call_id}/messages",
            json={"message": NOTE, "role": "system"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["data"]["details"] == {"role": "system"}

        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

        after = await app.api.post(
            f"/v1/calls/{call.call_id}/messages", json={"message": NOTE}
        )
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    said = [(t["role"], t["text"]) for t in ended["payload"]["transcript"]]
    assert ("agent", "Welcome back, valued customer.") in said, said
    injected = [e for e in events["data"] if e["event_type"] == "context.injected"]
    assert [e["payload"] for e in injected] == [{"message": NOTE, "role": "system"}]

    # The call is over: nothing is running it, so nothing can apply a message.
    assert after.status_code == 409, after.text
    assert after.json()["code"] == "call_not_live"


async def test_no_success_without_the_calls_acknowledgement(monkeypatch, redis_up):
    """A call that receives a request and never answers is a 504, and one that
    refuses it a 409 with its reason — never the 200 the endpoint used to give
    for a message nothing read."""
    from turncall.orchestrator import call_session
    from turncall.services import live_call_channel

    async def _unhelpful(body: dict) -> dict:
        if body["message"] == "refuse":
            raise ControlRefused("not today")
        await asyncio.Event().wait()  # received, never applied
        return {}

    monkeypatch.setattr(
        call_session, "call_handlers", lambda *_a, **_k: {"message": _unhelpful}
    )
    monkeypatch.setattr(live_call_channel, "ACK_TIMEOUT_SECONDS", 0.5)

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _live_call(app)
        url = f"/v1/calls/{call.call_id}/messages"
        refused = await app.api.post(url, json={"message": "refuse"})
        silent = await app.api.post(url, json={"message": "anything"})
        await call.hang_up()
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    assert refused.status_code == 409, refused.text
    assert "not today" in refused.json()["error"]
    assert silent.status_code == 504, silent.text
    assert silent.json()["code"] == "call_did_not_acknowledge"
    assert "context.injected" not in [e["event_type"] for e in events["data"]]
