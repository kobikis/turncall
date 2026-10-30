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
