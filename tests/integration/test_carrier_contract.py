"""The carrier contract (#201, ADR-0022): one agent, one script, every carrier.

Past its inbound edge a call must not know which carrier it is on, so the same
agent config and the same caller script have to produce the same outcome, the
same call events and the same transfer request whichever carrier the number
sits on. Each test here runs once per entry in `CARRIERS`; a carrier is added
by adding its edge — how to bind a number on it and how it rings one — and
every assertion below then holds it to what Twilio does.

Driven through Seam A (`tests/harness`): the real app, stub providers, a fake
carrier. Nothing here names a carrier's URL or wire format.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, ToolCall, run_app, telnyx
from tests.harness import place_inbound_call as place_twilio_call

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
OPERATOR = "+15559990000"


@dataclass(frozen=True)
class CarrierEdge:
    """What the contract needs from a carrier: bind a number, ring it.

    The call `ring` returns has the harness's call shape (`say`,
    `wait_for_agent_speech`, `closed`, `hang_up`, `finish`, `call_id`,
    `call_sid`, `agent_utterances`).

    `hangs_up_by_command`: whether ending the call takes a REST hang-up. On
    Twilio the media stream closing ends the call; on Telnyx it does not, and
    a stream left behind is a silent, billed line.
    """

    name: str
    bind: Callable[[Harness, str], Awaitable[str]]
    ring: Callable[[Harness, str, str], Awaitable[Any]]
    hangs_up_by_command: bool = False


TWILIO = CarrierEdge(
    name="twilio",
    bind=lambda app, agent_id: app.bind_number(agent_id),
    ring=lambda app, from_number, to_number: place_twilio_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
)

TELNYX = CarrierEdge(
    name="telnyx",
    bind=lambda app, agent_id: telnyx.bind_number(app.api, app.carrier, agent_id),
    ring=lambda app, from_number, to_number: telnyx.place_inbound_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
    hangs_up_by_command=True,
)

CARRIERS = [pytest.param(edge, id=edge.name) for edge in (TWILIO, TELNYX)]
# Telnyx transfers arrive with #220.
TRANSFER_CARRIERS = [pytest.param(TWILIO, id=TWILIO.name)]


class _MemoryRedis:
    """Where a transfer parks its intent for the carrier's callbacks. CI runs
    no Redis, and the contract is about the carrier, not the store."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": name,
        "parameters_schema": {"type": "object", "properties": {}},
    }


AGENT = {
    **HARNESS_AGENT,
    "first_message": "Hello from the contract.",
    "tools": [_tool("end_call"), _tool("transfer_call")],
}


async def _events(app: Harness, call_id: str) -> list[str]:
    response = await app.api.get(f"/v1/calls/{call_id}/events")
    assert response.status_code == 200, response.text
    return [e["event_type"] for e in response.json()["data"]]


def _in_order(seen: list[str], expected: list[str]) -> bool:
    """Whether `expected` occurs in `seen` in that order (others between)."""
    rest = iter(seen)
    return all(any(e == s for s in rest) for e in expected)


@pytest.mark.parametrize("edge", CARRIERS)
async def test_the_agent_ends_the_call(edge: CarrierEdge, monkeypatch) -> None:
    script = Script(
        caller=("Please end the call.",),
        agent=(ToolCall("end_call", {"reason": "caller asked"}), "Goodbye!"),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        listed = (await app.api.get("/v1/phone-numbers")).json()["data"]
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.closed.wait()
        await call.finish()
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        events = await _events(app, call.call_id)

    assert [n["carrier"] for n in listed if n["e164_number"] == number] == [edge.name]
    assert call.agent_utterances() == 2  # the greeting, the goodbye
    hung_up = [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")]
    assert hung_up == ([call.call_sid] if edge.hangs_up_by_command else [])
    assert ended["status"] == "completed"
    # App-side recording (ADR-0005) never learns the carrier either.
    assert ended["recording_status"] == "completed"
    assert ended["recording_url"]
    assert ended["ended_reason"] == "assistant_ended_call"
    assert ended["provider_call_sid"] == call.call_sid
    said = [(t["role"], t["text"]) for t in ended["transcript"]]
    assert ("customer", "Please end the call.") in said
    assert ("agent", "Goodbye!") in said
    # `call.ended` is the delivered webhook above. The event log's own
    # `call.ended` row is the carrier's end-of-call report, stored only when it
    # beats the pipeline's finalize — a race, so not part of the contract.
    expected = ["call.initializing", "call.started", "tool.result"]
    assert _in_order(events, expected), events


@pytest.mark.parametrize("edge", TRANSFER_CARRIERS)
async def test_a_cold_transfer(edge: CarrierEdge, monkeypatch) -> None:
    import turncall.storage.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda: _MemoryRedis())
    script = Script(
        caller=("Put me through to a person.",),
        agent=(
            ToolCall("transfer_call", {"target_number": OPERATOR}),
            "Connecting you now.",
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.say(400)
        transfer = await app.carrier.wait_for("transfer", timeout=10)
        # The carrier bridges the caller to the operator: the media stream
        # ends, and the call is over as far as the agent is concerned.
        await call.hang_up()
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        events = await _events(app, call.call_id)
        transferred = app.sink.of("call.transferred")

    assert transfer.params["call_sid"] == call.call_sid
    assert transfer.params["target_number"] == OPERATOR
    assert transfer.params["whisper_url"] is None  # cold: no operator briefing
    assert transfer.params["transfer_message"] is None
    assert [e["payload"]["success"] for e in transferred] == [True]
    assert transferred[0]["payload"]["transfer_mode"] == "cold"
    assert ended["ended_reason"] == "transferred"
    expected = ["call.started", "call.transferred"]  # call.ended: see above
    assert _in_order(events, expected), events
