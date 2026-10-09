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

    `hangs_up_by_command`: whether ending the call takes a REST hang-up, on
    the caller. On Twilio the media stream closing ends the call. On Telnyx it
    does not, and a stream left behind is a silent, billed line; on Asterisk
    the media is a channel of its own beside the caller's, so TurnCall hangs
    the caller's channel up itself.

    `hangs_up_after_the_caller`: whether that hang-up is sent even when the
    caller ended the call. Telnyx's media edge cannot tell why its stream
    ended, so it always hangs up; Telnyx answers `90018` for a call already
    over, which counts as hung up. Asterisk learns of the caller's hang-up
    from ARI and leaves the caller's channel alone.
    """

    name: str
    bind: Callable[[Harness, str], Awaitable[str]]
    ring: Callable[[Harness, str, str], Awaitable[Any]]
    hangs_up_by_command: bool = False
    hangs_up_after_the_caller: bool = False


TWILIO = CarrierEdge(
    name="twilio",
    bind=lambda app, agent_id: app.bind_number(agent_id),
    ring=lambda app, from_number, to_number: place_twilio_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
)
ASTERISK = CarrierEdge(
    name="asterisk",
    bind=lambda app, agent_id: app.bind_asterisk_number(agent_id),
    ring=lambda app, from_number, to_number: app.ring_asterisk(from_number, to_number),
    hangs_up_by_command=True,
)

TELNYX = CarrierEdge(
    name="telnyx",
    bind=lambda app, agent_id: telnyx.bind_number(app.api, app.carrier, agent_id),
    ring=lambda app, from_number, to_number: telnyx.place_inbound_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
    hangs_up_by_command=True,
    hangs_up_after_the_caller=True,
)

CARRIERS = [pytest.param(edge, id=edge.name) for edge in (TWILIO, TELNYX, ASTERISK)]
# Telnyx transfers and DTMF arrive with #220.
TRANSFER_CARRIERS = [pytest.param(edge, id=edge.name) for edge in (TWILIO, ASTERISK)]
DTMF_CARRIERS = TRANSFER_CARRIERS


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
    # `end_call` records `call.ended` before its own tool result is stored
    # (that is written off the reply's path), so only presence is the
    # contract for the tool. A carrier that reports the end itself (Twilio's
    # status callback) adds a later `call.ended`; one that does not, does not.
    expected = ["call.initializing", "call.started", "call.ended"]
    assert _in_order(events, expected), events
    assert "tool.result" in events


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

    # The transferred caller is the operator's now: never hung up.
    assert app.carrier.actions_of("hang_up") == []
    assert transfer.params["call_sid"] == call.call_sid
    assert transfer.params["target_number"] == OPERATOR
    assert transfer.params["whisper_url"] is None  # cold: no operator briefing
    assert transfer.params["transfer_message"] is None
    assert [e["payload"]["success"] for e in transferred] == [True]
    assert transferred[0]["payload"]["transfer_mode"] == "cold"
    assert ended["ended_reason"] == "transferred"
    # `call.ended` is the webhook above; only a carrier that reports the end
    # itself (Twilio's status callback) also leaves one in the event log.
    assert _in_order(events, ["call.started", "call.transferred"]), events


@pytest.mark.parametrize("edge", CARRIERS)
async def test_the_caller_hangs_up(edge: CarrierEdge, monkeypatch) -> None:
    script = Script(caller=(), agent=())
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.hang_up()
        ended = (await app.sink.wait_for("call.ended"))["payload"]

    hung_up = [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")]
    # They hung up, not us.
    assert hung_up == ([call.call_sid] if edge.hangs_up_after_the_caller else [])
    assert ended["status"] == "completed"
    assert ended["ended_reason"] == "customer_ended_call"
    assert ended["provider_call_sid"] == call.call_sid


@pytest.mark.parametrize("edge", DTMF_CARRIERS)
async def test_the_api_hangs_up_and_sends_dtmf(edge: CarrierEdge, monkeypatch) -> None:
    """Live call control reaches the carrier's REST side, on the caller."""
    async with run_app(
        Script(caller=(), agent=()),
        set_env=monkeypatch.setenv,
        set_attr=monkeypatch.setattr,
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        sent = await app.api.post(
            f"/v1/calls/{call.call_id}/dtmf", json={"digits": "12#"}
        )
        dtmf = await app.carrier.wait_for("send_dtmf")
        ended = await app.api.post(f"/v1/calls/{call.call_id}/end", json={})
        hang_up = await app.carrier.wait_for("hang_up")
        # The carrier ends the caller's call; its media stream goes with it.
        await call.hang_up()
        webhook = (await app.sink.wait_for("call.ended"))["payload"]

    assert sent.status_code == 200, sent.text
    assert (dtmf.params["call_sid"], dtmf.params["digits"]) == (call.call_sid, "12#")
    assert ended.status_code == 200, ended.text
    assert [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")] == [
        call.call_sid
    ]
    assert hang_up.params["call_sid"] == call.call_sid
    assert webhook["status"] == "completed"
    assert webhook["ended_reason"] == "assistant_ended_call"
