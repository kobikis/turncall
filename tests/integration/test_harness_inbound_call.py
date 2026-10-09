"""One complete inbound Twilio call through the running app (#214).

Everything a carrier and a subscriber would see, and nothing else: the number
is bound through the API (which configures it on the carrier), Twilio posts a
signed webhook and opens the media stream, the caller speaks, the agent
answers with a tool call and a goodbye, the stream closes, and `call.ended`
reaches the subscriber. Providers are the harness's stubs, the carrier its
fake. This is the proof the harness works; later tickets copy its shape.
"""

import pytest

from tests.harness import HARNESS_AGENT, Script, ToolCall, place_inbound_call, run_app

pytestmark = pytest.mark.integration

SCRIPT = Script(
    caller=("Please end the call.",),
    agent=(ToolCall("end_call", {"reason": "caller asked"}), "Goodbye!"),
)
AGENT = {
    **HARNESS_AGENT,
    "first_message": "Hello from the harness.",
    "tools": [
        {
            "name": "end_call",
            "description": "End the call",
            "parameters_schema": {"type": "object", "properties": {}},
        }
    ],
}


async def test_an_inbound_call_from_webhook_to_call_ended(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        configured = app.carrier.wait_for("configure_number")
        assert (
            (await configured)
            .params["voice_url"]
            .endswith("/webhooks/twilio/voice/inbound")
        )

        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        await call.say(400)
        await call.closed.wait()  # the agent hung up: the stream ended
        await call.finish()
        ended = await app.sink.wait_for("call.ended")
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    # What the caller heard: the greeting and the goodbye, then a closed line.
    assert call.agent_utterances() == 2
    # The agent's hangup is the stream closing, not a REST request.
    assert app.carrier.actions_of("hang_up") == []

    assert ended["_signature_valid"] is True
    assert ended["call_id"] == call.call_id
    payload = ended["payload"]
    assert payload["status"] == "completed"
    assert payload["ended_reason"] == "assistant_ended_call"
    assert payload["provider_call_sid"] == call.call_sid
    said = [(t["role"], t["text"]) for t in payload["transcript"]]
    assert ("customer", "Please end the call.") in said
    assert ("agent", "Goodbye!") in said

    types = [e["event_type"] for e in events["data"]]
    for expected in ("call.initializing", "call.started", "tool.result", "call.ended"):
        assert expected in types, types
    assert app.sink.of("call.started"), "call.started never reached the subscriber"
