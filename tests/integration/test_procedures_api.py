"""An agent's procedures through the agent API (#442).

`procedures` is part of the agent config: accepted on create and update,
stored with the agent version, read back as written. The shape is checked at
save (the graph itself is #444's rule); `move_to_step` is the platform's to
offer and is refused in `tools`. Seam A: the real app over Postgres.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.harness import Harness, Script, run_app

pytestmark = pytest.mark.integration

LINE_STATUS = {
    "name": "line_status",
    "description": "Look up the caller's line status",
    "parameters_schema": {"type": "object", "properties": {}},
    "webhook_url": "https://example.com/line_status",
}
# A step's tool is one of the agent's (#447).
AGENT = {"system_prompt": "You are Maya.", "tools": [LINE_STATUS]}


def procedure(name: str = "no_internet", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Caller has no internet connection at home",
        "start": "call",
        "window_depth": 1,
        "steps": [
            {
                "id": "lights",
                "kind": "ask",
                "start": True,
                "text": "Ask which lights on the router are on.",
                "moves": [{"to": "restart", "when": "the internet light is red"}],
            },
            {
                "id": "restart",
                "kind": "instruct",
                "text": "Have the caller restart the router.",
                "tool": "line_status",
                "moves": [{"to": "technician", "when": "it is still red"}],
            },
            {
                "id": "technician",
                "kind": "escalate",
                "text": "Explain a technician will be booked.",
                "escalate": {
                    "transfer_call": {
                        "target_number": "+15550009999",
                        "transfer_mode": "warm",
                    }
                },
            },
        ],
        **extra,
    }


async def _agent(app: Harness, config: dict[str, Any]) -> httpx.Response:
    return await app.api.post("/v1/agents", json={"name": "support", "config": config})


async def _read(app: Harness, agent_id: str) -> dict[str, Any]:
    response = await app.api.get(f"/v1/agents/{agent_id}")
    assert response.status_code == 200, response.text
    return dict(response.json()["data"]["config"])


def _refusal(response: httpx.Response) -> str:
    assert response.status_code == 422, response.text
    return response.text


async def test_procedures_are_stored_with_the_agent_version(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await _agent(app, {**AGENT, "procedures": [procedure()]})
        assert created.status_code == 201, created.text
        v1 = created.json()["data"]["id"]
        stored = await _read(app, v1)

        renamed = procedure("router_down", start="on_request", window_depth=2)
        updated = await app.api.put(
            f"/v1/agents/{v1}", json={"config": {**AGENT, "procedures": [renamed]}}
        )
        assert updated.status_code == 200, updated.text
        edited = await _read(app, v1)

        v2 = (await _agent(app, AGENT)).json()["data"]["id"]
        without = await _read(app, v2)

    [one] = stored["procedures"]
    assert one["name"] == "no_internet"
    assert one["start"] == "call" and one["window_depth"] == 1
    assert [s["id"] for s in one["steps"]] == ["lights", "restart", "technician"]
    assert one["steps"][0]["moves"] == [
        {"to": "restart", "when": "the internet light is red"}
    ]
    assert one["steps"][1]["tool"] == "line_status"
    assert one["steps"][2]["escalate"]["transfer_call"]["target_number"] == (
        "+15550009999"
    )
    [two] = edited["procedures"]
    assert (two["name"], two["start"], two["window_depth"]) == (
        "router_down",
        "on_request",
        2,
    )
    # Another version of the agent carries its own: none.
    assert without["procedures"] == []


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"start": "whenever"}, "start"),
        ({"window_depth": -1}, "window_depth"),
        ({"name": "No Internet"}, "name"),
        ({"steps": []}, "steps"),
        ({"surprise": True}, "surprise"),
    ],
)
async def test_a_misshapen_procedure_is_refused_at_save(
    monkeypatch, change: dict[str, Any], named: str
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        refused = await _agent(app, {**AGENT, "procedures": [procedure(**change)]})

    assert named in _refusal(refused)


async def test_a_step_of_an_unknown_kind_is_refused_at_save(monkeypatch) -> None:
    body = procedure()
    body["steps"][0] = {**body["steps"][0], "kind": "guess"}
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        refused = await _agent(app, {**AGENT, "procedures": [body]})

    assert "kind" in _refusal(refused)


async def test_move_to_step_is_not_listed_in_tools(monkeypatch) -> None:
    tool = {
        "name": "move_to_step",
        "description": "Move",
        "parameters_schema": {"type": "object", "properties": {}},
    }
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        refused = await _agent(app, {**AGENT, "tools": [tool]})

    assert "offered automatically to an agent with procedures" in _refusal(refused)


def _step_tool(tool: str) -> dict[str, Any]:
    body = procedure()
    body["steps"][1] = {**body["steps"][1], "tool": tool}
    return body


async def test_a_step_tool_that_is_not_the_agents_is_refused_at_save(
    monkeypatch,
) -> None:
    """#447: a step names a tool the agent has, or a built-in."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        refused = await _agent(app, {**AGENT, "procedures": [_step_tool("line_stats")]})
        agent_id = (await _agent(app, AGENT)).json()["data"]["id"]
        updated = await app.api.put(
            f"/v1/agents/{agent_id}",
            json={"config": {**AGENT, "procedures": [_step_tool("line_stats")]}},
        )
        builtin = await _agent(app, {**AGENT, "procedures": [_step_tool("end_call")]})

    for response in (refused, updated):
        assert (
            "procedures[0] 'no_internet': step 'restart' names tool 'line_stats', "
            "which is not one of the agent's tools or a built-in"
        ) in _refusal(response)
    assert builtin.status_code == 201, builtin.text


async def test_a_broken_graph_is_refused_at_create_and_update_naming_the_step(
    monkeypatch,
) -> None:
    # #444: the graph rule runs at save, so the mistake is found here rather
    # than on a call.
    broken = procedure("router_down")
    broken["steps"][1] = {
        **broken["steps"][1],
        "moves": [{"to": "technican", "when": "it is still red"}],
    }
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await _agent(app, {**AGENT, "procedures": [broken]})
        sound = await _agent(app, {**AGENT, "procedures": [procedure()]})
        assert sound.status_code == 201, sound.text
        updated = await app.api.put(
            f"/v1/agents/{sound.json()['data']['id']}",
            json={"config": {**AGENT, "procedures": [procedure(), broken]}},
        )

    said = "step 'restart' moves to 'technican', which does not exist"
    assert f"procedures[0] 'router_down': {said}" in _refusal(created)
    assert f"procedures[1] 'router_down': {said}" in _refusal(updated)


def _escalating(action: dict[str, Any]) -> dict[str, Any]:
    escalating = procedure()
    escalating["steps"][2] = {**escalating["steps"][2], "escalate": action}
    return escalating


async def test_an_escalate_step_that_does_not_say_what_it_does_is_refused(
    monkeypatch,
) -> None:
    # #448: reaching an escalate step runs its action, so the action is
    # checked at save: one of the two, each in the tool's own shape.
    both = {
        "transfer_call": {"target_number": "+15550009999"},
        "handoff_to_agent": {"agent_id": "7b0c8d4e-1f2a-4b3c-9d8e-6f5a4b3c2d1e"},
    }
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        none = await _agent(app, {**AGENT, "procedures": [_escalating({})]})
        two = await _agent(app, {**AGENT, "procedures": [_escalating(both)]})
        not_e164 = await _agent(
            app,
            {
                **AGENT,
                "procedures": [
                    _escalating({"transfer_call": {"target_number": "the desk"}})
                ],
            },
        )

    where = "procedures[0] 'no_internet': step 'technician'"
    assert f"{where} escalates with no action" in _refusal(none)
    assert f"{where} names both a transfer and a handoff" in _refusal(two)
    assert "target_number" in _refusal(not_e164)


async def test_a_handoff_escalation_names_an_agent_of_the_project(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        billing = await _agent(app, AGENT)
        assert billing.status_code == 201, billing.text
        billing_id = billing.json()["data"]["id"]
        to_billing = _escalating({"handoff_to_agent": {"agent_id": billing_id}})
        stranger = "7b0c8d4e-1f2a-4b3c-9d8e-6f5a4b3c2d1e"
        to_nobody = _escalating({"handoff_to_agent": {"agent_id": stranger}})

        created = await _agent(app, {**AGENT, "procedures": [to_billing]})
        refused = await _agent(app, {**AGENT, "procedures": [to_nobody]})
        updated = await app.api.put(
            f"/v1/agents/{billing_id}",
            json={"config": {**AGENT, "procedures": [to_nobody]}},
        )

    assert created.status_code == 201, created.text
    said = (
        f"procedures[0] 'no_internet': step 'technician' hands off to "
        f"'{stranger}', which is not an agent in this project"
    )
    assert said in _refusal(refused)
    assert said in _refusal(updated)
    assert refused.json()["code"] == "invalid_agent_config"
    # The field is the step's own, as a client puts the error beside it.
    assert refused.json()["details"]["field"] == (
        "procedures[0].steps[2].escalate.handoff_to_agent.agent_id"
    )


async def test_an_s2s_agents_read_says_its_procedures_are_ignored(monkeypatch):
    s2s = {**AGENT, "pipeline_mode": "s2s", "s2s": {"provider": "google"}}
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await _agent(app, {**s2s, "procedures": [procedure()]})
        assert created.status_code == 201, created.text
        read = await app.api.get(f"/v1/agents/{created.json()['data']['id']}")

    [warning] = [
        w
        for w in read.json()["data"]["warnings"]
        if w["code"] == "procedures_ignored_on_s2s"
    ]
    assert warning["procedures"] == ["no_internet"]
    assert warning["provider"] == "google"
