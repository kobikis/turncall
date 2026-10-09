"""Weighted agents and start/finish events on a campaign (#234), through the
running app, the worker's own pass and the fake carrier, as in
`test_campaigns.py`."""

from collections import Counter
from typing import Any

import pytest

from tests.harness import run_app
from tests.integration.limits_support import AGENT, SCRIPT, limited_project
from tests.integration.test_campaigns import (
    _create,
    _dials,
    _end,
    _numbers,
    _pass,
    _targets,
    _unpaced,
)

pytestmark = pytest.mark.integration


async def _weighted(
    app: Any, agents: list[dict[str, Any]], number_id: str, numbers: list[str]
) -> Any:
    return await app.api.post(
        "/v1/campaigns",
        json={
            "name": "a/b",
            "agents": agents,
            "from_number_id": number_id,
            "timezone": "America/New_York",
            "targets": [{"number": n} for n in numbers],
        },
    )


async def test_targets_are_split_by_weight_the_same_way_every_time(
    monkeypatch, redis_env
):
    _unpaced(monkeypatch)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        first, caller_id, number_id = await limited_project(app)
        second = await app.create_agent(AGENT)
        weights = [
            {"agent_id": first, "weight": 30},
            {"agent_id": second, "weight": 70},
        ]
        numbers = _numbers(40)
        r = await _weighted(app, weights, number_id, numbers)
        assert r.status_code == 201, r.text
        campaign = r.json()["data"]
        await _pass()
        targets = await _targets(app, campaign["id"])
        placed_as = {}
        for number, target in targets.items():
            call = (await app.api.get(f"/v1/calls/{target['call_id']}")).json()
            placed_as[number] = call["data"]["active_agent_id"]

    assert campaign["agent_id"] is None
    assert campaign["agents"] == weights
    from turncall.services.weighted_routing import pick_agent_by_weight

    assigned = {n: t["agent_id"] for n, t in targets.items()}
    # By number, through the phone-number A/B rule: a pure function of the
    # number and the weights, so the same list splits the same way every time...
    assert assigned == {n: str(pick_agent_by_weight(weights, n)) for n in numbers}
    assert set(Counter(assigned.values())) == {first, second}
    # ...and each call is placed as the agent its target was given.
    assert placed_as == assigned
    assert len(_dials(app, caller_id)) == len(numbers)


async def test_create_takes_exactly_one_way_to_pick_agents(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, number_id = await limited_project(app)
        numbers = _numbers(1)
        half = [
            {"agent_id": agent_id, "weight": 50},
            {"agent_id": "00000000-0000-0000-0000-000000000001", "weight": 50},
        ]
        unknown = await _weighted(app, half, number_id, numbers)
        short = await _weighted(
            app,
            [
                {"agent_id": agent_id, "weight": 50},
                {"agent_id": agent_id, "weight": 40},
            ],
            number_id,
            numbers,
        )
        body = {
            "name": "x",
            "agent_id": agent_id,
            "agents": half,
            "from_number_id": number_id,
            "timezone": "America/New_York",
            "targets": [{"number": numbers[0]}],
        }
        both = await app.api.post("/v1/campaigns", json=body)
        neither = await app.api.post(
            "/v1/campaigns",
            json={k: v for k, v in body.items() if k not in ("agent_id", "agents")},
        )
        single = await _create(app, agent_id, number_id, numbers)
        targets = await _targets(app, single)

    assert unknown.status_code == 404
    assert "00000000-0000-0000-0000-000000000001" in unknown.text
    assert short.status_code == 422
    assert "sum to 100" in short.text
    assert both.status_code == 422
    assert neither.status_code == 422
    assert "exactly one of agent_id and agents" in both.text
    assert targets[numbers[0]]["agent_id"] == agent_id


async def test_start_and_finish_are_announced_once_each(monkeypatch, redis_env):
    _unpaced(monkeypatch)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        numbers = _numbers(2)
        campaign_id = await _create(app, agent_id, number_id, numbers)

        await _pass(workers=2)
        started = await app.sink.wait_for("campaign.started")
        assert app.sink.of("campaign.completed") == []
        for dial in _dials(app, caller_id):
            await _end(app, dial, "ringing", "no-answer")
        await _pass(workers=2)
        completed = await app.sink.wait_for("campaign.completed")
        await _pass(workers=2)  # a finished campaign announces nothing more

    mine = [
        e
        for e in app.sink.events
        if e["event"].startswith("campaign.") and e["campaign_id"] == campaign_id
    ]
    assert [e["event"] for e in mine] == ["campaign.started", "campaign.completed"]
    for event in (started, completed):
        assert event["_signature_valid"]
        assert event["campaign_id"] == campaign_id
        assert event["call_id"] is None
        assert event["agent_id"] == agent_id
        assert "id" not in event["payload"]
    assert started["payload"]["status"] == "running"
    assert started["payload"]["targets"]["pending"] == 2
    assert completed["payload"]["status"] == "completed"
    assert completed["payload"]["targets"]["completed"] == 2
    assert completed["payload"]["completed_at"] is not None
