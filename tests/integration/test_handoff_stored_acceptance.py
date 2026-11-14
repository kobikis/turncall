"""Handoff targets and stored agents go through agent acceptance (#480).

Over the running app. A stored agent saved before a newer rule existed is
made here the only way one can be: written straight into `agents`, since the
API refuses it now. A handoff to it fails in its prepare step, so the model —
or the live API, as a 409 — reads the rule's reason and the call keeps its
agent; an inbound call to a number routed to it is refused at the carrier,
its live-call slot given back, the refusal logged.
"""

from __future__ import annotations

import asyncio
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest
from loguru import logger

from tests.harness import (
    HARNESS_AGENT,
    Given,
    Script,
    ToolCall,
    place_inbound_call,
    post_signed,
    run_app,
)
from tests.integration.limits_support import ring
from tests.integration.test_harness_call_handoff import redis_up  # noqa: F401
from turncall.services import limits

pytestmark = pytest.mark.integration

XAI_REASON = "base_url is not supported for the xai provider"
FIRST_PROMPT = "You are the first agent."
HANDOFF = {
    "name": "handoff_to_agent",
    "description": "Hand the call to another agent",
    "parameters_schema": {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    },
}


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


async def _stored_before_the_rule(app: Any, name: str = "billing") -> str:
    """An agent the API accepted, then given an `llm` the xAI rule now refuses
    — as if it had been saved before the rule existed."""
    from turncall.storage.database import get_session_factory
    from turncall.storage.models import AgentRow

    agent_id = await app.create_agent(
        {**HARNESS_AGENT, "system_prompt": "You are billing."}, name=name
    )
    async with get_session_factory()() as session:
        row = await session.get(AgentRow, uuid.UUID(agent_id))
        row.config_blob = {
            **row.config_blob,
            "llm": {"provider": "xai", "base_url": "https://gw.example/v1"},
        }
        await session.commit()
    return agent_id


def _remembering(seen: list[Given], answer: str):
    def _answer(given: Given) -> str:
        seen.append(given)
        return answer

    return _answer


async def _first_agent_call(app: Any):
    number = await app.bind_number(
        await app.create_agent(
            {
                **HARNESS_AGENT,
                "system_prompt": FIRST_PROMPT,
                "first_message": "Hello.",
                "tools": [HANDOFF],
            }
        )
    )
    call = await place_inbound_call(
        app.carrier, app.api, from_number="+15550001234", to_number=number
    )
    await call.wait_for_agent_speech(1)
    return call


async def test_a_handoff_to_a_refused_target_says_why_and_keeps_the_agent(
    monkeypatch, warnings
):
    target: list[str] = []
    after: list[Given] = []
    script = Script(
        caller=("I have a billing question.",),
        agent=(
            lambda _given: ToolCall("handoff_to_agent", {"agent_id": target[0]}),
            _remembering(after, "Still me."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _first_agent_call(app)
        target.append(await _stored_before_the_rule(app))
        before = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

        await call.say(400)
        await call.wait_for_agent_speech(2)  # "Still me."
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        await call.hang_up()
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    [given] = after
    tool_results = [m["content"] for m in given if m["role"] == "tool"]
    assert any(XAI_REASON in str(r) for r in tool_results), tool_results
    # The prompt and tools the model holds are still the first agent's.
    system = next(m["content"] for m in given if m["role"] == "system")
    assert FIRST_PROMPT in system
    assert given.tools == ["handoff_to_agent"]
    # Nothing was committed.
    assert record["active_agent_id"] == before["active_agent_id"] != target[0]
    assert record["status"] == before["status"] == "in_progress"
    assert "call.agent_handoff" not in [e["event_type"] for e in events["data"]]
    assert any("origin=handoff" in w and XAI_REASON in w for w in warnings)


async def test_the_live_api_handoff_to_a_refused_target_is_409_with_the_reason(
    monkeypatch,
    redis_up,  # noqa: F811
):
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        call = await _first_agent_call(app)
        billing = await _stored_before_the_rule(app)
        refused = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff", json={"target_agent_id": billing}
        )
        record = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]
        await call.hang_up()

    assert refused.status_code == 409, refused.text
    assert XAI_REASON in refused.json()["error"]
    assert record["active_agent_id"] != billing
    assert record["status"] == "in_progress"


async def _live_slots(project_id: str) -> int:
    from turncall.storage.redis import get_redis

    return await get_redis().zcard(limits._live_calls_key(uuid.UUID(project_id)))


@pytest.mark.parametrize("weighted", [False, True], ids=["agent", "a_b_weights"])
async def test_a_call_to_a_refused_stored_agent_is_refused_at_the_carrier(
    monkeypatch, redis_env, warnings, weighted
):
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        broken = await _stored_before_the_rule(app)
        number = await app.bind_number(broken)
        if weighted:
            numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
            number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
            other = await _stored_before_the_rule(app, name="other")
            r = await app.api.put(
                f"/v1/phone-numbers/{number_id}/routing",
                json={
                    "weights": [
                        {"agent_id": broken, "weight": 50},
                        {"agent_id": other, "weight": 50},
                    ]
                },
            )
            assert r.status_code == 200, r.text

        voice_url = app.carrier.number(number).voice_url
        answered = await post_signed(
            app.api, app.carrier, voice_url, ring(app.carrier, number)
        )
        await asyncio.sleep(0)
        slots = await _live_slots(app.project_id)
        calls = (await app.api.get("/v1/calls")).json()["data"]

    assert answered.status_code == 200, answered.text
    twiml = ET.fromstring(answered.text)
    assert twiml.findtext("Say") == "This agent is not available."
    assert twiml.find("Hangup") is not None
    assert twiml.find("Connect") is None  # never answered with a stream
    assert slots == 0
    assert calls == []  # refused before a row was written
    assert any("origin=stored_call" in w and XAI_REASON in w for w in warnings)
