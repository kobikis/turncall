"""A procedure that cannot resolve the problem ends safely (#448).

An `escalate` step names its action, and reaching it runs that action
through the path the tools take: a transfer is `transfer_call`'s (so it works
wherever that does), a handoff is `handoff_to_agent`'s. The call's
`procedures` then reads `completed: false, escalated_at: <step>`. A handoff —
from an escalate step, the `handoff_to_agent` tool or `POST
/v1/calls/{id}/handoff` — ends the first agent's running procedures, recorded
as `procedure.left` where each stood, and the target agent's own procedures
apply as at a call's start. Seam A: the real app, stub providers, a fake
carrier.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.twilio import place_inbound_call
from tests.integration.test_harness_call_handoff import redis_up  # noqa: F401
from tests.integration.test_harness_call_procedures import (
    _procedure_events,
    _prompt,
    _remembering,
    _until,
    _windows,
)

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
TECHNICIANS = "+15550009999"
BILLING_PROMPT = "You are the billing agent."
END_CALL = {
    "name": "end_call",
    "description": "End the call",
    "parameters_schema": {"type": "object", "properties": {}},
}


def _no_internet(escalate: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "no_internet",
        "description": "Caller has no internet connection at home",
        "steps": [
            {
                "id": "lights",
                "kind": "ask",
                "start": True,
                "text": "Ask which lights on the router are on.",
                "moves": [
                    {"to": "restart", "when": "the internet light is red"},
                    {"to": "technician", "when": "no light is on at all"},
                ],
            },
            {
                "id": "restart",
                "kind": "instruct",
                "text": "Have the caller restart the router.",
                "moves": [
                    {"to": "resolved", "when": "the light turns green"},
                    {"to": "technician", "when": "it stays red"},
                ],
            },
            {"id": "resolved", "kind": "end", "text": "Close politely."},
            {
                "id": "technician",
                "kind": "escalate",
                "text": "Tell the caller a person will take it from here.",
                "escalate": escalate,
            },
        ],
    }


REFUND = {
    "name": "refund",
    "description": "Caller wants money back",
    "steps": [
        {
            "id": "amount",
            "kind": "ask",
            "start": True,
            "text": "Ask how much the refund is for.",
            "moves": [{"to": "done", "when": "the caller said an amount"}],
        },
        {"id": "done", "kind": "end", "text": "Confirm the refund."},
    ],
}


def _move(to: str) -> ToolCall:
    return ToolCall("move_to_step", {"procedure": "no_internet", "to": to})


def _summary(ended: dict[str, Any]) -> list[dict[str, Any]]:
    return ended["payload"]["procedures"]


def _started(procedure: str, at: str) -> tuple[str, dict[str, Any]]:
    """A procedure starting with its agent, at its start step (an `ask`)."""
    payload = {"procedure": procedure, "from": None, "to": at, "kind": "ask"}
    return ("procedure.step", payload)


async def test_a_transfer_escalate_step_transfers_the_call(monkeypatch):
    import turncall.storage.redis as redis_module
    from tests.integration.test_carrier_contract import _MemoryRedis

    # Where the transfer parks its intent for Twilio's callbacks.
    monkeypatch.setattr(redis_module, "get_redis", lambda: _MemoryRedis())
    escalate = {
        "transfer_call": {
            "target_number": TECHNICIANS,
            "transfer_message": "Connecting you to a technician.",
        }
    }
    script = Script(
        caller=("Nothing is on, not one light.",),
        agent=(("A technician will take it from here.", _move("technician")),),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [_no_internet(escalate)],
                }
            )
        )
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        heard = await call.complete_transfer(answered_by="human")
        ended = await app.sink.wait_for("call.ended")
        events = await _procedure_events(app, call.call_id, at_least=2)

    # The step's transfer, as `transfer_call` would have made it.
    assert heard.target == TECHNICIANS
    assert heard.caller == ["Connecting you to a technician."]
    assert heard.bridged
    assert ended["payload"]["ended_reason"] == "transferred"
    assert [e["payload"]["target_number"] for e in app.sink.of("call.transferred")] == [
        TECHNICIANS
    ]
    # Recorded where it escalated: not completed, and no `procedure.left` —
    # the procedure ended at its escalate step, nothing left it.
    assert events == [
        _started("no_internet", "lights"),
        (
            "procedure.step",
            {
                "procedure": "no_internet",
                "from": "lights",
                "to": "technician",
                "kind": "escalate",
            },
        ),
    ]
    assert _summary(ended) == [
        {
            "name": "no_internet",
            "path": ["lights", "technician"],
            "completed": False,
            "ended_at": "technician",
            "escalated_at": "technician",
            "refused_moves": 0,
        }
    ]


async def test_a_warm_escalation_briefs_the_operator_from_the_call(monkeypatch):
    # `briefing` takes what `transfer_call` takes: a summary written when the
    # operator answers, from the conversation so far.
    import turncall.services.llm_text as llm_text
    import turncall.storage.redis as redis_module
    from tests.integration.test_carrier_contract import _MemoryRedis

    async def summarize(_llm: Any, messages: list[dict[str, str]], **_: Any) -> Any:
        transcript = messages[-1]["content"].removeprefix("Transcript:\n")
        return SimpleNamespace(text=f"Summary. {transcript}")

    monkeypatch.setattr(llm_text, "complete_text", summarize)
    # One store: the whisper reads the intent the transfer parked.
    redis = _MemoryRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: redis)
    escalate = {
        "transfer_call": {
            "target_number": TECHNICIANS,
            "transfer_mode": "warm",
            "briefing": {"from_summary": True},
        }
    }
    script = Script(
        caller=("Nothing is on, not one light.",),
        agent=(("A technician will take it from here.", _move("technician")),),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await app.api.post(
            "/v1/agents",
            json={
                "name": "support",
                "config": {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [_no_internet(escalate)],
                },
            },
        )
        assert created.status_code == 201, created.text
        stored = created.json()["data"]["config"]["procedures"][0]["steps"][3]
        number = await app.bind_number(created.json()["data"]["id"])
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        heard = await call.complete_transfer(answered_by="human")
        await app.sink.wait_for("call.ended")

    assert stored["escalate"]["transfer_call"]["briefing"] == {"from_summary": True}
    assert heard.target == TECHNICIANS and heard.bridged
    [briefing] = heard.operator
    assert briefing.startswith("Summary. ")
    assert "Nothing is on, not one light." in briefing
    [transferred] = app.sink.of("call.transferred")
    assert transferred["payload"]["transfer_mode"] == "warm"


async def _billing(app: Any) -> str:
    return await app.create_agent(
        {
            **HARNESS_AGENT,
            "system_prompt": BILLING_PROMPT,
            "tools": [END_CALL],
            "procedures": [REFUND],
        },
        name="billing",
    )


async def _call(app: Any, config: dict[str, Any]) -> Any:
    number = await app.bind_number(
        await app.create_agent({**HARNESS_AGENT, "first_message": "Hello.", **config})
    )
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    return call


def _as_billing(given: Given) -> None:
    """The turn the target agent answered: its own prompt, tools and
    procedure, and nothing of the first agent's."""
    assert BILLING_PROMPT in _prompt(given)
    [window] = _windows(given)
    assert "refund" in window and "no_internet" not in window, window
    assert sorted(given.tools) == ["end_call", "move_to_step"]


async def test_a_handoff_escalate_step_hands_off_to_the_named_agent(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.", "Still red.", "I'd like a refund."),
        agent=(
            _remembering(given, ("Let's restart it.", _move("restart"))),
            ("I'll hand you to billing.", _move("technician")),
            _remembering(given, "Billing here. How much?"),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        billing = await _billing(app)
        escalate = {"handoff_to_agent": {"agent_id": billing, "reason": "billing"}}
        call = await _call(app, {"procedures": [_no_internet(escalate)]})
        for turn in (2, 3, 4):
            await call.say(400)
            await call.wait_for_agent_speech(turn)
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        events = await _procedure_events(app, call.call_id, at_least=4)

    first_turn, billings_turn = given
    assert "no_internet" in _windows(first_turn)[0]
    _as_billing(billings_turn)
    assert record["active_agent_id"] == billing
    handoffs = app.sink.of("call.agent_handoff")
    assert [e["payload"]["target_agent_id"] for e in handoffs] == [billing]
    # Escalated, never left: it ended at its escalate step. Then billing's
    # own procedure started, as at a call's start.
    assert [t for t, _ in events] == ["procedure.step"] * 4
    assert events[-1] == _started("refund", "amount")
    no_internet, refund = _summary(ended)
    assert no_internet["path"] == ["lights", "restart", "technician"]
    assert no_internet["escalated_at"] == "technician"
    assert no_internet["completed"] is False
    assert refund["path"] == ["amount"]


async def test_a_handoff_by_the_tool_leaves_the_running_procedures(monkeypatch):
    given: list[Given] = []
    ids: dict[str, str] = {}
    handoff = {"name": "handoff_to_agent", "description": "Hand the call over"}
    escalate = {"transfer_call": {"target_number": TECHNICIANS}}
    script = Script(
        caller=(
            "The internet light is red.",
            "Actually, it's about my bill.",
            "Fifty dollars.",
        ),
        agent=(
            ("Let's restart it.", _move("restart")),
            lambda _given: ToolCall("handoff_to_agent", {"agent_id": ids["billing"]}),
            # The handoff's result: the target agent speaks first.
            "Billing here. How much?",
            _remembering(given, "Refunding fifty dollars."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        ids["billing"] = await _billing(app)
        call = await _call(
            app,
            {
                "tools": [{**handoff, "parameters_schema": {"type": "object"}}],
                "procedures": [_no_internet(escalate)],
            },
        )
        for turn in (2, 3):
            await call.say(400)
            await call.wait_for_agent_speech(turn)
        await call.say(400)
        await _until(lambda: len(given) >= 1)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        events = await _procedure_events(app, call.call_id, at_least=4)

    _as_billing(given[0])
    assert events[-2:] == [
        (
            "procedure.left",
            {"procedure": "no_internet", "from": "restart", "reason": "handoff"},
        ),
        _started("refund", "amount"),
    ]
    assert _summary(ended)[0] == {
        "name": "no_internet",
        "path": ["lights", "restart"],
        "completed": False,
        "ended_at": "restart",
        "escalated_at": None,
        "refused_moves": 0,
    }


async def test_a_handoff_through_the_api_leaves_the_running_procedures(
    monkeypatch,
    redis_up,  # noqa: F811
):
    given: list[Given] = []
    escalate = {"transfer_call": {"target_number": TECHNICIANS}}
    script = Script(
        caller=("I'd like a refund.",),
        agent=(_remembering(given, "Billing here. How much?"),),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _call(app, {"procedures": [_no_internet(escalate)]})
        billing = await _billing(app)
        handed = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff", json={"target_agent_id": billing}
        )
        assert handed.status_code == 200, handed.text
        await call.say(400)
        await _until(lambda: len(given) >= 1)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        events = await _procedure_events(app, call.call_id, at_least=3)

    assert sorted(handed.json()["data"]["details"]["tools"]) == [
        "end_call",
        "move_to_step",
    ]
    _as_billing(given[0])
    # Left at its start, where it stood: it never moved.
    assert events == [
        _started("no_internet", "lights"),
        (
            "procedure.left",
            {"procedure": "no_internet", "from": "lights", "reason": "handoff"},
        ),
        _started("refund", "amount"),
    ]
    left, _refund = _summary(ended)
    assert (left["path"], left["ended_at"], left["completed"]) == (
        ["lights"],
        "lights",
        False,
    )
