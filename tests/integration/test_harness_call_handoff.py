"""`POST /v1/calls/{id}/handoff` is applied by the live call (#216).

Over the running app: an inbound Twilio call on one agent, a handoff to another
through the API while it is live, and the agent's next reply decided by the
prompt and tools its model was actually given — so what the caller hears
proves the pipeline switched, and the call record and `call.ended` prove the
record agrees. Before this the endpoint wrote the record and nothing in the
call changed. Then the refusals it shares with `/messages`: 409 when the call
is gone, 504 when it does not acknowledge, 409 with the call's reason.
"""

import asyncio
import os

import pytest
from redis.asyncio import Redis

from tests.harness import HARNESS_AGENT, Given, Script, place_inbound_call, run_app
from turncall.services.live_call_channel import HANDOFF, ControlRefused

pytestmark = pytest.mark.integration

BILLING_PROMPT = "You are the billing agent."
BILLING = {
    **HARNESS_AGENT,
    "system_prompt": BILLING_PROMPT,
    "tools": [
        {
            "name": "end_call",
            "description": "End the call",
            "parameters_schema": {"type": "object", "properties": {}},
        }
    ],
}


def _answer_as_whoever_i_am(given: Given) -> str:
    prompt = next((m["content"] for m in given if m["role"] == "system"), "")
    if BILLING_PROMPT in prompt and given.tools == ["end_call"]:
        return "Billing here."
    return f"Still the first agent (tools: {given.tools})."


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


async def test_an_api_handoff_switches_the_live_call(monkeypatch, redis_up):
    script = Script(
        caller=("I have a billing question.",), agent=(_answer_as_whoever_i_am,)
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _live_call(app)
        billing = await app.create_agent(BILLING, name="billing")

        handed = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff",
            json={"target_agent_id": billing, "reason": "billing question"},
        )
        assert handed.status_code == 200, handed.text
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

        after = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff", json={"target_agent_id": billing}
        )
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    # The 200 describes what the call applied.
    assert handed.json()["data"]["details"] == {
        "target_agent_id": billing,
        "target_assistant_name": "billing",
        "tools": ["end_call"],
    }
    # The pipeline switched: the next reply was the billing agent's own.
    said = [(t["role"], t["text"]) for t in ended["payload"]["transcript"]]
    assert ("agent", "Billing here.") in said, said
    # And the record agrees, on the read and on the webhook.
    assert record["active_agent_id"] == billing
    assert record["status"] == "in_progress"
    assert ended["agent_id"] == billing
    handoffs = [e for e in events["data"] if e["event_type"] == "call.agent_handoff"]
    assert [e["payload"]["target_agent_id"] for e in handoffs] == [billing]
    assert handoffs[0]["payload"]["reason"] == "billing question"

    # The call is over: nothing is running it, so nothing can switch it.
    assert after.status_code == 409, after.text
    assert after.json()["code"] == "call_not_live"


async def test_no_handoff_without_the_calls_acknowledgement(monkeypatch, redis_up):
    """A call that refuses is a 409 with its reason and one that never answers
    a 504 — and neither moves the record, which the endpoint used to write
    whatever the call did."""
    from turncall.orchestrator import call_session
    from turncall.services import live_call_channel

    async def _unhelpful(body: dict) -> dict:
        if body["reason"] == "refuse":
            raise ControlRefused("not today")
        await asyncio.Event().wait()  # received, never applied
        return {}

    monkeypatch.setattr(
        call_session, "call_handlers", lambda *_a, **_k: {HANDOFF: _unhelpful}
    )
    monkeypatch.setattr(live_call_channel, "ACK_TIMEOUT_SECONDS", 0.5)

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _live_call(app)
        billing = await app.create_agent(BILLING, name="billing")
        url = f"/v1/calls/{call.call_id}/handoff"
        refused = await app.api.post(
            url, json={"target_agent_id": billing, "reason": "refuse"}
        )
        silent = await app.api.post(url, json={"target_agent_id": billing})
        unknown = await app.api.post(
            url, json={"target_agent_id": "00000000-0000-0000-0000-00000000beef"}
        )
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        await call.hang_up()
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    assert refused.status_code == 409, refused.text
    assert "not today" in refused.json()["error"]
    assert silent.status_code == 504, silent.text
    assert silent.json()["code"] == "call_did_not_acknowledge"
    assert unknown.status_code == 404, unknown.text
    assert record["active_agent_id"] != billing
    assert "call.agent_handoff" not in [e["event_type"] for e in events["data"]]
