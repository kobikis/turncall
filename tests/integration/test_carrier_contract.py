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
from types import SimpleNamespace
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, ToolCall, run_app, telnyx
from tests.harness import place_inbound_call as place_twilio_call
from tests.harness.media_call import TransferHeard

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
OPERATOR = "+15559990000"


@dataclass(frozen=True)
class CarrierEdge:
    """What the contract needs from a carrier: bind a number, ring it.

    The call `ring` returns has the harness's call shape (`say`,
    `wait_for_agent_speech`, `closed`, `hang_up`, `finish`, `call_id`,
    `call_sid`, `agent_utterances`), and plays the carrier's side of a
    transfer (`complete_transfer`) so a transfer is compared by what each end
    heard, not by the carrier's wire format.

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


class _MemoryRedis:
    """Where a transfer parks its intent for the carrier's callbacks. CI runs
    no Redis, and the contract is about the carrier, not the store."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": name,
        "parameters_schema": {"type": "object", "properties": {}},
    }


AGENT = {
    **HARNESS_AGENT,
    "first_message": "Hello from the contract.",
    "tools": [_tool("end_call"), _tool("transfer_call"), _tool("send_dtmf")],
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


@dataclass(frozen=True)
class Transferred:
    """A transfer, as the two ends heard it and as the backend was told."""

    heard: TransferHeard
    ended: dict[str, Any]  # the call.ended payload
    events: list[str]  # the call's event log
    transferred: list[dict[str, Any]]  # call.transferred payloads
    answered: list[dict[str, Any]]  # transfer.answered payloads
    caller_hung_up_by_us: bool


async def _transfer(
    edge: CarrierEdge, monkeypatch, args: dict[str, Any], *, answered_by: str | None
) -> Transferred:
    """The agent transfers the caller with `args`; the carrier plays the
    transfer out, with the operator's line answered by `answered_by`."""
    import turncall.storage.redis as redis_module

    redis = _MemoryRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    script = Script(
        caller=("Put me through to a person.",),
        agent=(ToolCall("transfer_call", args), "Connecting you now."),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.say(400)
        heard = await call.complete_transfer(answered_by=answered_by)
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        events = await _events(app, call.call_id)
    hung_up = [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")]
    return Transferred(
        heard=heard,
        ended=ended,
        events=events,
        transferred=[e["payload"] for e in app.sink.of("call.transferred")],
        answered=[e["payload"] for e in app.sink.of("transfer.answered")],
        caller_hung_up_by_us=call.call_sid in hung_up,
    )


def _assert_transferred(result: Transferred, mode: str) -> None:
    assert [t["success"] for t in result.transferred] == [True]
    assert result.transferred[0]["transfer_mode"] == mode
    # Attempted is enough: a transfer nobody answered still reads transferred.
    assert result.ended["ended_reason"] == "transferred"
    expected = ["call.started", "call.transferred"]  # call.ended: see above
    assert _in_order(result.events, expected), result.events


@pytest.mark.parametrize("edge", CARRIERS)
async def test_a_cold_transfer(edge: CarrierEdge, monkeypatch) -> None:
    """The caller hears the transfer message, then ringing, then the operator:
    no briefing on a cold transfer, and the agent's stream ending is not the
    call ending."""
    result = await _transfer(
        edge,
        monkeypatch,
        {"target_number": OPERATOR, "transfer_message": "Transferring you."},
        answered_by="human",
    )

    assert result.heard == TransferHeard(
        target=OPERATOR, caller=["Transferring you."], ringing=True, bridged=True
    )
    # The caller is with the operator: nothing of ours hung their leg up.
    assert not result.caller_hung_up_by_us
    _assert_transferred(result, "cold")


@pytest.mark.parametrize("edge", CARRIERS)
async def test_a_warm_transfer_briefs_the_operator(edge, monkeypatch) -> None:
    """The operator hears a summary of the conversation so far before being
    connected; the caller hears ringing, never the briefing."""
    import turncall.services.llm_text as llm_text

    async def summarize(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        transcript = messages[-1]["content"].removeprefix("Transcript:\n")
        return SimpleNamespace(text=f"Summary. {transcript}")

    monkeypatch.setattr(llm_text, "complete_text", summarize)
    result = await _transfer(
        edge,
        monkeypatch,
        {
            "target_number": OPERATOR,
            "transfer_mode": "warm",
            "transfer_message": "Transferring you.",
            "briefing": {"from_summary": True},
        },
        answered_by="human",
    )

    heard = result.heard
    assert (heard.target, heard.caller) == (OPERATOR, ["Transferring you."])
    assert (heard.ringing, heard.bridged) == (True, True)
    [briefing] = heard.operator
    assert briefing.startswith("Summary. ")
    assert "Caller: Put me through to a person." in briefing
    assert result.answered == [{"target_number": OPERATOR, "answered_by": "human"}]
    assert not result.caller_hung_up_by_us
    _assert_transferred(result, "warm")


@pytest.mark.parametrize("edge", CARRIERS)
async def test_a_warm_transfer_to_voicemail_is_reported_not_blocked(
    edge, monkeypatch
) -> None:
    """ADR-0009: machine detection notifies and never holds the bridge, so the
    caller can leave the operator a message (the briefing plays into the
    voicemail first — the known wart)."""
    result = await _transfer(
        edge,
        monkeypatch,
        {
            "target_number": OPERATOR,
            "transfer_mode": "warm",
            "briefing": "Caller wants billing.",
        },
        answered_by="machine",
    )

    assert result.heard == TransferHeard(
        target=OPERATOR,
        operator=["Caller wants billing."],
        ringing=True,
        bridged=True,
    )
    assert result.answered == [{"target_number": OPERATOR, "answered_by": "machine"}]
    _assert_transferred(result, "warm")


@pytest.mark.parametrize("edge", CARRIERS)
async def test_a_warm_transfer_nobody_answers(edge, monkeypatch) -> None:
    """No answer: the caller hears the fallback message and the call ends —
    the agent does not come back (ADR-0009)."""
    result = await _transfer(
        edge,
        monkeypatch,
        {
            "target_number": OPERATOR,
            "transfer_mode": "warm",
            "transfer_message": "Transferring you.",
            "briefing": "Caller wants billing.",
            "fallback_message": "Sorry, nobody is free.",
        },
        answered_by=None,
    )

    assert result.heard == TransferHeard(
        target=OPERATOR,
        caller=["Transferring you.", "Sorry, nobody is free."],
        ringing=True,
    )
    assert result.answered == []
    _assert_transferred(result, "warm")


@pytest.mark.parametrize("edge", CARRIERS)
async def test_the_agent_sends_dtmf(edge: CarrierEdge, monkeypatch) -> None:
    script = Script(
        caller=("Press one and pound for me.",),
        agent=(ToolCall("send_dtmf", {"digits": "1w#"}), "Done."),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        call = await edge.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.say(400)
        dtmf = await app.carrier.wait_for("send_dtmf", timeout=10)
        await call.hang_up()
        await app.sink.wait_for("call.ended")
        events = await _events(app, call.call_id)

    assert dtmf.params == {"call_sid": call.call_sid, "digits": "1w#"}
    assert "dtmf.sent" in events
