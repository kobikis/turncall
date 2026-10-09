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

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from tests.harness import (
    HARNESS_AGENT,
    Harness,
    Script,
    ToolCall,
    answer_outbound_call,
    run_app,
    telnyx,
)
from tests.harness import place_inbound_call as place_twilio_call
from tests.harness import post_signed as post_twilio
from tests.harness.carrier import CarrierAction
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

    `pick_up` is the callee answering a call the app dialled (a recorded
    `dial` action), returning the same call shape; `no_answer` is that call
    ringing out unanswered. None on a carrier that places no outbound calls
    (Asterisk).

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

    `speaks_transfer_messages`: whether a transfer can carry a message for the
    caller (and a fallback). Asterisk hands the caller to the PBX's transfer
    dialplan, which says nothing of ours, so it refuses one.
    """

    name: str
    bind: Callable[[Harness, str], Awaitable[str]]
    ring: Callable[[Harness, str, str], Awaitable[Any]]
    pick_up: Callable[[Harness, CarrierAction], Awaitable[Any]] | None = None
    no_answer: Callable[[Harness, CarrierAction], Awaitable[Any]] | None = None
    hangs_up_by_command: bool = False
    hangs_up_after_the_caller: bool = False
    speaks_transfer_messages: bool = True


async def _twilio_no_answer(app: Harness, dial: CarrierAction) -> None:
    """Twilio reports a dial nobody picked up to the dial's status callback."""
    response = await post_twilio(
        app.api,
        app.carrier,
        dial.params["status_callback_url"],
        {
            "AccountSid": app.carrier.account_sid,
            "CallSid": dial.params["call_sid"],
            "CallStatus": "no-answer",
            "From": dial.params["from_number"],
            "To": dial.params["to_number"],
            "Direction": "outbound-api",
        },
    )
    response.raise_for_status()


TWILIO = CarrierEdge(
    name="twilio",
    bind=lambda app, agent_id: app.bind_number(agent_id),
    ring=lambda app, from_number, to_number: place_twilio_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
    pick_up=lambda app, dial: answer_outbound_call(app.carrier, app.api, dial),
    no_answer=_twilio_no_answer,
)
ASTERISK = CarrierEdge(
    name="asterisk",
    bind=lambda app, agent_id: app.bind_asterisk_number(agent_id),
    ring=lambda app, from_number, to_number: app.ring_asterisk(from_number, to_number),
    hangs_up_by_command=True,
    speaks_transfer_messages=False,
)

TELNYX = CarrierEdge(
    name="telnyx",
    bind=lambda app, agent_id: telnyx.bind_number(app.api, app.carrier, agent_id),
    ring=lambda app, from_number, to_number: telnyx.place_inbound_call(
        app.carrier, app.api, from_number=from_number, to_number=to_number
    ),
    pick_up=lambda app, dial: telnyx.answer_outbound_call(app.carrier, app.api, dial),
    no_answer=lambda app, dial: telnyx.not_answered(app.carrier, app.api, dial),
    hangs_up_by_command=True,
    hangs_up_after_the_caller=True,
)

CARRIERS = [pytest.param(edge, id=edge.name) for edge in (TWILIO, TELNYX, ASTERISK)]
# Asterisk transfers cold only: the PBX's dialplan has no briefing to play.
WARM_TRANSFER_CARRIERS = [pytest.param(edge, id=edge.name) for edge in (TWILIO, TELNYX)]
# Asterisk refuses outbound calls (422): its PBX dials, not TurnCall.
OUTBOUND_CARRIERS = WARM_TRANSFER_CARRIERS


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


async def _wait_for_event(
    app: Harness, call_id: str, event_type: str, *, timeout: float = 10.0
) -> None:
    """Until the call's event log holds `event_type` — stored and committed,
    which is later than the carrier seeing the request that caused it."""
    async with asyncio.timeout(timeout):
        while event_type not in await _events(app, call_id):
            await asyncio.sleep(0.05)


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


async def _dial(app: Harness, edge: CarrierEdge) -> tuple[str, CarrierAction]:
    """Bind a number on the carrier and dial CALLER from it through the API,
    exactly as for any carrier; the call's id and the carrier's dial."""
    agent_id = await app.create_agent(AGENT)
    number = await edge.bind(app, agent_id)
    numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
    response = await app.api.post(
        "/v1/calls/outbound",
        json={
            "agent_id": agent_id,
            "from_number_id": next(
                n["id"] for n in numbers if n["e164_number"] == number
            ),
            "to_number": CALLER,
        },
    )
    assert response.status_code == 201, response.text
    call = response.json()["data"]
    dials = app.carrier.actions_of("dial")
    assert [d.params["from_number"] for d in dials] == [number]
    assert call["provider_call_sid"] == dials[0].params["call_sid"]
    return call["id"], dials[0]


@pytest.mark.parametrize("edge", OUTBOUND_CARRIERS)
async def test_an_outbound_call(edge: CarrierEdge, monkeypatch) -> None:
    """Dialled through the API, picked up, ended by the agent (#218)."""
    script = Script(
        caller=("Please end the call.",),
        agent=(ToolCall("end_call", {"reason": "callee asked"}), "Goodbye!"),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call_id, dial = await _dial(app, edge)
        call = await edge.pick_up(app, dial)
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.closed.wait()
        await call.finish()
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        events = await _events(app, call_id)

    assert call.call_id == call_id
    assert call.agent_utterances() == 2  # the greeting, the goodbye
    hung_up = [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")]
    assert hung_up == ([call.call_sid] if edge.hangs_up_by_command else [])
    assert ended["direction"] == "outbound"
    if edge.name == "telnyx":  # what a transfer on this call dials on (#220)
        assert ended["metadata"]["telnyx_connection_id"] == dial.params["connection_id"]
    assert ended["to_number"] == CALLER
    assert ended["status"] == "completed"
    assert ended["recording_status"] == "completed"
    assert ended["ended_reason"] == "assistant_ended_call"
    assert ended["provider_call_sid"] == call.call_sid
    said = [(t["role"], t["text"]) for t in ended["transcript"]]
    assert ("customer", "Please end the call.") in said
    assert ("agent", "Goodbye!") in said
    assert _in_order(events, ["call.started", "tool.result"]), events


@pytest.mark.parametrize("edge", OUTBOUND_CARRIERS)
async def test_an_outbound_call_nobody_answers(edge: CarrierEdge, monkeypatch) -> None:
    """What a campaign's retry policy reads: `customer_did_not_answer`."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call_id, dial = await _dial(app, edge)
        await edge.no_answer(app, dial)
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]

    assert read["status"] == "no_answer"
    assert read["ended_reason"] == "customer_did_not_answer"


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
    message = ["Transferring you."] if edge.speaks_transfer_messages else []
    args = {"target_number": OPERATOR}
    if message:
        args["transfer_message"] = message[0]
    result = await _transfer(edge, monkeypatch, args, answered_by="human")

    assert result.heard == TransferHeard(
        target=OPERATOR, caller=message, ringing=True, bridged=True
    )
    # The caller is with the operator: nothing of ours hung their leg up.
    assert not result.caller_hung_up_by_us
    _assert_transferred(result, "cold")


@pytest.mark.parametrize("edge", WARM_TRANSFER_CARRIERS)
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


@pytest.mark.parametrize("edge", WARM_TRANSFER_CARRIERS)
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


@pytest.mark.parametrize("edge", WARM_TRANSFER_CARRIERS)
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


@pytest.mark.parametrize("edge", CARRIERS)
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
    # Telnyx's media edge hangs up again when the stream ends, not knowing why
    # it did; Telnyx answers "already ended" (see `hangs_up_after_the_caller`).
    again = [call.call_sid] if edge.hangs_up_after_the_caller else []
    hung_up = [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")]
    assert hung_up == [call.call_sid, *again]
    assert hang_up.params["call_sid"] == call.call_sid
    assert webhook["status"] == "completed"
    assert webhook["ended_reason"] == "assistant_ended_call"


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
        # The carrier records the request before `call_control.send_dtmf` has
        # written `dtmf.sent`; hanging up in between ends the call under it.
        await _wait_for_event(app, call.call_id, "dtmf.sent")
        await call.hang_up()
        await app.sink.wait_for("call.ended")
        events = await _events(app, call.call_id)

    assert dtmf.params == {"call_sid": call.call_sid, "digits": "1w#"}
    assert "dtmf.sent" in events
