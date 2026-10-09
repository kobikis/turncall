"""Pause, resume and cancel, and two workers racing one claim (#232).

Driven like `test_campaigns.py`: the worker's own pass, a fixed clock, the
fake carrier's `dial` actions as the count of what was dialled. The crash half
— a worker process killed mid-dial — is `test_campaign_worker_crash.py`.
"""

import asyncio
from collections import Counter
from datetime import timedelta
from typing import Any

import pytest

from tests.harness import Script, answer_outbound_call, run_app
from tests.integration.limits_support import SCRIPT, limited_project
from tests.integration.test_campaigns import (
    NOW,
    _clock,
    _create,
    _dials,
    _end,
    _numbers,
    _pass,
    _targets,
    _unpaced,
)

pytestmark = pytest.mark.integration


async def _control(app: Any, campaign_id: str, action: str) -> Any:
    return await app.api.post(f"/v1/campaigns/{campaign_id}/{action}")


async def _campaign(app: Any, campaign_id: str) -> dict:
    return (await app.api.get(f"/v1/campaigns/{campaign_id}")).json()["data"]


async def test_pause_holds_new_dials_and_leaves_a_live_call_up(monkeypatch, redis_env):
    """One live call at a time: the first is answered and talking when the
    campaign is paused. It stays up, its ending is still recorded, and nothing
    new is dialled until resume — even with the slot it held free again."""
    _unpaced(monkeypatch)
    async with run_app(
        Script(agent=("Hello there.",)),
        set_env=monkeypatch.setenv,
        set_attr=monkeypatch.setattr,
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app, live_calls=1)
        numbers = _numbers(3)
        campaign_id = await _create(app, agent_id, number_id, numbers)

        await _pass()
        [first] = _dials(app, caller_id)
        call = await answer_outbound_call(app.carrier, app.api, first)
        await call.wait_for_agent_speech(1)

        paused = await _control(app, campaign_id, "pause")
        await _pass()
        await asyncio.sleep(0.3)
        still_up = not call.closed.is_set() and not app.sink.of("call.ended")

        await call.hang_up()
        await app.sink.wait_for("call.ended")
        await _pass()
        await _pass()
        while_paused = await _targets(app, campaign_id)
        dials_while_paused = len(_dials(app, caller_id))

        resumed = await _control(app, campaign_id, "resume")
        await _pass()
        after_resume = await _targets(app, campaign_id)

    assert paused.status_code == 200, paused.text
    assert paused.json()["data"]["status"] == "paused"
    assert still_up
    assert app.carrier.actions_of("hang_up") == []
    assert dials_while_paused == 1
    assert while_paused[numbers[0]]["status"] == "completed"
    assert while_paused[numbers[0]]["ended_reason"] == "customer_ended_call"
    assert {while_paused[n]["status"] for n in numbers[1:]} == {"pending"}

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["status"] == "running"
    assert [d.params["to_number"] for d in _dials(app, caller_id)] == numbers[:2]
    assert after_resume[numbers[1]]["status"] == "calling"


async def test_cancel_ends_the_campaign_and_lets_its_live_call_finish(
    monkeypatch, redis_env
):
    _unpaced(monkeypatch)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app, live_calls=1)
        numbers = _numbers(3)
        campaign_id = await _create(app, agent_id, number_id, numbers)

        await _pass()
        [live] = _dials(app, caller_id)
        cancelled = await _control(app, campaign_id, "cancel")
        await _end(app, live, "ringing", "completed")
        await _pass()
        await _pass()
        targets = await _targets(app, campaign_id)
        campaign = await _campaign(app, campaign_id)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert cancelled.json()["data"]["targets"]["cancelled"] == 2
    assert app.carrier.actions_of("hang_up") == []
    assert [d.params["to_number"] for d in _dials(app, caller_id)] == numbers[:1]
    # Settled after the cancel: the call it had placed still reports its end.
    assert targets[numbers[0]]["status"] == "completed"
    assert targets[numbers[0]]["ended_reason"] == "customer_ended_call"
    assert {targets[n]["status"] for n in numbers[1:]} == {"cancelled"}
    assert campaign["status"] == "cancelled"


async def test_control_moves_and_the_ones_refused(monkeypatch, redis_env):
    _unpaced(monkeypatch)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, number_id = await limited_project(app)
        queued = await _create(app, agent_id, number_id, _numbers(1))
        moves = {
            "pause": await _control(app, queued, "pause"),
            "pause again": await _control(app, queued, "pause"),
            # Never started, so it goes back to waiting for the worker.
            "resume": await _control(app, queued, "resume"),
            "resume again": await _control(app, queued, "resume"),
            "cancel": await _control(app, queued, "cancel"),
            "cancel again": await _control(app, queued, "cancel"),
            "resume cancelled": await _control(app, queued, "resume"),
            "pause cancelled": await _control(app, queued, "pause"),
        }
        missing = await _control(app, "00000000-0000-0000-0000-000000000000", "pause")
        announced = await app.sink.wait_for("campaign.cancelled")
        await asyncio.sleep(0.3)  # a second cancel would have announced by now

    # Cancelled is its own terminal event, sent once: by the cancel that moved it.
    mine = [
        e["event"]
        for e in app.sink.events
        if e["event"].startswith("campaign.") and e["campaign_id"] == queued
    ]
    assert mine == ["campaign.cancelled"]
    assert announced["payload"]["status"] == "cancelled"
    assert announced["payload"]["targets"]["cancelled"] == 1

    status = {k: (r.status_code, r.json()) for k, r in moves.items()}
    assert status["pause"][0] == status["pause again"][0] == 200
    assert status["pause again"][1]["data"]["status"] == "paused"
    assert status["resume"][1]["data"]["status"] == "queued"
    assert status["resume again"][0] == 200
    assert status["cancel again"][1]["data"]["status"] == "cancelled"
    for refused, wanted in (
        ("resume cancelled", "running"),
        ("pause cancelled", "paused"),
    ):
        code, body = status[refused]
        assert code == 409, body
        assert body["code"] == "invalid_state_transition"
        assert body["details"] == {"current_state": "cancelled", "target_state": wanted}
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("action", "left_as"), [("pause", "pending"), ("cancel", "cancelled")]
)
async def test_a_move_landing_between_claim_and_dial_dials_nothing(
    monkeypatch, redis_env, action, left_as
):
    """The claim is committed, the carrier not yet asked, when the operator
    acts: the worker loses `placing` and the carrier never hears of it."""
    _unpaced(monkeypatch)
    from turncall.storage.repositories import suppression_repo

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        [number] = _numbers(1)
        campaign_id = await _create(app, agent_id, number_id, [number])
        is_suppressed = suppression_repo.is_suppressed
        responses = []

        async def _operator_acts(*args: Any, **kwargs: Any) -> bool:
            if str(kwargs["project_id"]) == app.project_id:
                responses.append(await _control(app, campaign_id, action))
            return await is_suppressed(*args, **kwargs)

        monkeypatch.setattr(suppression_repo, "is_suppressed", _operator_acts)
        await _pass()
        monkeypatch.setattr(suppression_repo, "is_suppressed", is_suppressed)
        await _pass()
        targets = await _targets(app, campaign_id)

    assert [r.status_code for r in responses] == [200]
    assert _dials(app, caller_id) == []
    assert targets[number]["status"] == left_as
    assert targets[number]["attempts"] == 0


async def test_a_reclaimed_claim_is_dialled_by_one_worker_only(monkeypatch, redis_env):
    """Worker A claims a target and stalls before asking the carrier, past the
    claim timeout. Worker B's janitor takes the claim back and B dials it.
    When A wakes it dials nothing: `placing` is already B's."""
    _unpaced(monkeypatch)
    from turncall.campaigns.dialer import run_pass
    from turncall.config import get_settings
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import suppression_repo

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        numbers = _numbers(2)
        campaign_id = await _create(app, agent_id, number_id, numbers)
        is_suppressed = suppression_repo.is_suppressed
        a_stalled, a_may_go = asyncio.Event(), asyncio.Event()

        async def _stall_once(*args: Any, **kwargs: Any) -> bool:
            if str(kwargs["project_id"]) == app.project_id and not a_stalled.is_set():
                a_stalled.set()
                await a_may_go.wait()
            return await is_suppressed(*args, **kwargs)

        monkeypatch.setattr(suppression_repo, "is_suppressed", _stall_once)
        timeout = get_settings().campaigns.claim_timeout_seconds
        later = NOW + timedelta(seconds=timeout + 1)
        factory = get_session_factory()

        worker_a = asyncio.create_task(run_pass(factory, clock=_clock))
        await asyncio.wait_for(a_stalled.wait(), 5)
        await run_pass(factory, clock=lambda: later)  # worker B
        b_dialled = [d.params["to_number"] for d in _dials(app, caller_id)]
        a_may_go.set()
        await worker_a
        targets = await _targets(app, campaign_id)

    assert b_dialled == numbers
    assert Counter(d.params["to_number"] for d in _dials(app, caller_id)) == Counter(
        numbers
    )
    assert {t["status"] for t in targets.values()} == {"calling"}
    assert {t["attempts"] for t in targets.values()} == {1}
