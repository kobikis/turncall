"""What a subscriber hears is what the call's event log holds (#312).

Every event leaves a call through one exit point (`events/emit.py`), where
redaction (#313) will stand. This pins what leaves over one full inbound
Twilio call: the events read back through the API, in sequence order, and the
webhooks the sink receives, in the order the app sent them, are the same
events with the same payloads. The difference is by design, and listed:
some events are only stored, and the comprehensive `call.ended` is only sent,
last (ADR-0006). `tests/unit/test_event_exit_point.py` is the source half.
"""

import pytest

from tests.harness import HARNESS_AGENT, Script, ToolCall, place_inbound_call, run_app

pytestmark = pytest.mark.integration

GREETING = "Hello from the harness."
ASKED = "Please end the call."
GOODBYE = "Goodbye, and thank you for calling."
SCRIPT = Script(
    caller=(ASKED,),
    agent=(ToolCall("end_call", {"reason": "caller asked"}), GOODBYE),
    # A tool's result and the reply after it are written by separate tasks.
    # Instant, they race for the next sequence number and for the wire, and
    # the order would be the scheduler's; a model's think time is not.
    think_ms=300,
)
AGENT = {
    **HARNESS_AGENT,
    "first_message": GREETING,
    "tools": [
        {
            "name": "end_call",
            "description": "End the call",
            "parameters_schema": {"type": "object", "properties": {}},
        }
    ],
}

# On the call's log, never sent (CLAUDE.md, "Webhook Events"). `call.ended`
# rows are the carrier's status and the agent's hangup marker; subscribers
# get the comprehensive one instead.
STORED_ONLY = {"call.ended", "metrics.turn", "delegation.completed"}
# What leaves this call, in order, on both sides.
LEAVES = [
    ("call.initializing", None),
    ("call.started", None),
    ("transcript.final", GREETING),
    ("transcript.final", ASKED),
    ("tool.result", None),
    ("transcript.final", GOODBYE),
    ("recording.ready", None),
]


async def test_the_webhooks_are_the_calls_event_log(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        await call.say(400)
        await call.closed.wait()  # the agent hung up
        await call.finish()
        await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    stored = sorted(read["data"], key=lambda e: e["sequence_number"])
    # Each webhook is a request of its own, sent concurrently, so arrival is
    # not the order: the envelope's timestamp is when the app sent it.
    sent = sorted(
        (e for e in app.sink.events if e["call_id"] == call.call_id),
        key=lambda e: e["timestamp"],
    )

    stored_and_sent = [
        (e["event_type"], e["payload"])
        for e in stored
        if e["event_type"] not in STORED_ONLY
    ]
    sent_from_the_log = [
        (e["event"], e["payload"]) for e in sent if e["event"] != "call.ended"
    ]
    assert sent_from_the_log == stored_and_sent

    def text(payload):
        return payload.get("text") if "role" in payload else None

    assert [(kind, text(payload)) for kind, payload in stored_and_sent] == LEAVES

    # `call.ended` is sent last, after the recording (ADR-0006), and is not
    # one of the log's `call.ended` rows: those stay on the log.
    ended = [e for e in sent if e["event"] == "call.ended"]
    assert ended, "call.ended never reached the subscriber"
    assert {e["event"] for e in sent[-len(ended) :]} == {"call.ended"}
    said = [(t["role"], t["text"]) for t in ended[0]["payload"]["transcript"]]
    assert ("customer", ASKED) in said and ("agent", GOODBYE) in said
    marker = {"reason": "caller asked", "source": "control"}
    assert any(e["payload"] == marker for e in stored)
    assert all(e["payload"] != marker for e in sent)

    assert all(e["_signature_valid"] for e in sent)
    assert len({e["event_id"] for e in sent}) == len(sent)
