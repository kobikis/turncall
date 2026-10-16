"""What a subscriber hears is what the call's event log holds (#312).

Every call event leaves through one exit point (`events/emit.py`), where
redaction (#313) will stand. This pins what leaves over one full inbound
Twilio call: the events read back through the API and the webhooks the sink
receives are the same events with the same payloads. The difference is by
design, and listed: some events are only stored, and the comprehensive
`call.ended` is only sent, last (ADR-0006). `tests/unit/test_event_exit_point.py`
is the source half.

Order is compared only where it is guaranteed. Each webhook is its own
background delivery, so arrival order is not send order; and a tool's result
and the reply after it are written by separate tasks that race for the next
sequence number, so the log's order between them is the scheduler's.
"""

import json
from collections import Counter
from datetime import datetime

import pytest

from tests.harness import HARNESS_AGENT, Script, ToolCall, place_inbound_call, run_app

pytestmark = pytest.mark.integration

GREETING = "Hello from the harness."
ASKED = "Please end the call."
GOODBYE = "Goodbye, and thank you for calling."
SCRIPT = Script(
    caller=(ASKED,),
    agent=(ToolCall("end_call", {"reason": "caller asked"}), GOODBYE),
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

# On the call's log, never sent (`events/emit.py`, CLAUDE.md "Webhook
# Events"). `call.ended` rows are the carrier's status and the agent's hangup
# marker; subscribers get the comprehensive one instead.
STORED_ONLY = {
    "call.ended",
    "metrics.turn",
    "delegation.completed",
    "voicemail.detected",
}
# What leaves this call, on both sides.
LEAVES = Counter(
    [
        ("call.initializing", None),
        ("call.started", None),
        ("transcript.final", GREETING),
        ("transcript.final", ASKED),
        ("tool.result", None),
        ("transcript.final", GOODBYE),
        ("recording.ready", None),
    ]
)


def _key(event_type: str, payload: dict) -> tuple[str, str]:
    """One event, comparable and countable: its type and its exact payload."""
    return event_type, json.dumps(payload, sort_keys=True)


def _sent_at(webhook: dict) -> datetime:
    return datetime.fromisoformat(webhook["timestamp"])


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

    stored = read["data"]
    sent = [e for e in app.sink.events if e["call_id"] == call.call_id]

    # The same events, each webhook carrying exactly its stored payload.
    stored_and_sent = Counter(
        _key(e["event_type"], e["payload"])
        for e in stored
        if e["event_type"] not in STORED_ONLY
    )
    sent_from_the_log = Counter(
        _key(e["event"], e["payload"]) for e in sent if e["event"] != "call.ended"
    )
    assert sent_from_the_log == stored_and_sent

    def text(payload: str) -> str | None:
        said = json.loads(payload)
        return said.get("text") if "role" in said else None

    assert Counter((kind, text(p)) for kind, p in stored_and_sent.elements()) == LEAVES

    # `call.ended` is sent after everything else (ADR-0006): the envelope's
    # timestamp is when the delivery went out. It is not one of the log's
    # `call.ended` rows: those stay on the log.
    ended = [e for e in sent if e["event"] == "call.ended"]
    others = [e for e in sent if e["event"] != "call.ended"]
    assert ended, "call.ended never reached the subscriber"
    last_other = max(_sent_at(e) for e in others)
    assert all(_sent_at(e) > last_other for e in ended), sent
    said = [(t["role"], t["text"]) for t in ended[0]["payload"]["transcript"]]
    assert ("customer", ASKED) in said and ("agent", GOODBYE) in said
    marker = {"reason": "caller asked", "source": "control"}
    assert any(e["payload"] == marker for e in stored)
    assert all(e["payload"] != marker for e in sent)

    assert all(e["_signature_valid"] for e in sent)
    assert len({e["event_id"] for e in sent}) == len(sent)
