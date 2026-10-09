"""Campaigns, through the running app and the campaign worker's own pass (#229).

The worker is driven one pass at a time with a fixed clock, against the fake
carrier, so "dialled exactly once" is a count of the carrier's `dial` actions
rather than a claim about which function ran. Calls end the way Twilio ends
them — status callbacks to the dial's own callback URL — which is also what
frees their live-call slots.
"""

import asyncio
import secrets
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.harness import Script, answer_outbound_call, post_signed, run_app
from tests.harness.carrier import CarrierAction
from tests.integration.limits_support import AGENT, SCRIPT, limited_project

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


async def _pass(workers: int = 1) -> None:
    """One pass of the campaign worker — or of several, side by side."""
    from turncall.campaigns.dialer import run_pass
    from turncall.storage.database import get_session_factory

    factory = get_session_factory()
    await asyncio.gather(*(run_pass(factory, clock=_clock) for _ in range(workers)))


async def _create(app: Any, agent_id: str, number_id: str, numbers: list[str]) -> str:
    r = await app.api.post(
        "/v1/campaigns",
        json={
            "name": "renewals",
            "agent_id": agent_id,
            "from_number_id": number_id,
            "timezone": "America/New_York",
            "targets": [{"number": n} for n in numbers],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _targets(app: Any, campaign_id: str) -> dict[str, dict]:
    r = await app.api.get(f"/v1/campaigns/{campaign_id}/targets")
    assert r.status_code == 200, r.text
    return {t["number"]: t for t in r.json()["data"]}


async def _end(app: Any, dial: CarrierAction, *statuses: str) -> None:
    """Twilio reporting a dialled call's progress, ending in its last status."""
    for status in statuses:
        r = await post_signed(
            app.api,
            app.carrier,
            dial.params["status_callback_url"],
            {
                "AccountSid": app.carrier.account_sid,
                "CallSid": dial.params["call_sid"],
                "CallStatus": status,
                "Direction": "outbound-api",
                "From": dial.params["from_number"],
                "To": dial.params["to_number"],
            },
        )
        assert r.status_code == 204, r.text


def _dials(app: Any, from_number: str) -> list[CarrierAction]:
    """The carrier's dials from this test's number. A pass serves every
    campaign in the database, so a campaign another run left behind may dial
    too — from a number of its own."""
    return [
        a
        for a in app.carrier.actions_of("dial")
        if a.params["from_number"] == from_number
    ]


def _numbers(count: int) -> list[str]:
    return [f"+1555{secrets.randbelow(10**7):07d}" for _ in range(count)]


async def test_every_target_is_dialled_once_and_a_suppressed_one_never(
    monkeypatch, redis_env
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        numbers = _numbers(4)
        suppressed = numbers[2]
        r = await app.api.post("/v1/suppression-list", json={"number": suppressed})
        assert r.status_code == 201, r.text
        campaign_id = await _create(app, agent_id, number_id, numbers)

        await _pass(workers=2)
        dialled_first = _dials(app, caller_id)
        await _pass(workers=2)  # nothing new is due while the calls are live
        mid = await _targets(app, campaign_id)
        for dial, ending in zip(
            dialled_first,
            (("ringing", "no-answer"), ("busy",), ("ringing", "completed")),
            strict=True,
        ):
            await _end(app, dial, *ending)
        await _pass()
        await _pass()  # a finished campaign is left alone
        targets = await _targets(app, campaign_id)
        campaign = (await app.api.get(f"/v1/campaigns/{campaign_id}")).json()["data"]

    dialled = Counter(a.params["to_number"] for a in _dials(app, caller_id))
    assert dialled == Counter(n for n in numbers if n != suppressed)
    assert len(dialled_first) == 3

    assert mid[suppressed]["status"] == "suppressed"
    assert "suppression list" in mid[suppressed]["error"]
    assert mid[suppressed]["attempts"] == 0
    assert {mid[n]["status"] for n in numbers if n != suppressed} == {"calling"}

    by_dial = {d.params["to_number"]: d for d in dialled_first}
    reasons = {n: targets[n]["ended_reason"] for n in numbers if n != suppressed}
    assert sorted(reasons.values()) == [
        "customer_busy",
        "customer_did_not_answer",
        "customer_ended_call",
    ]
    for number in by_dial:
        assert targets[number]["status"] == "completed"
        assert targets[number]["attempts"] == 1
        assert targets[number]["call_id"] is not None
    assert campaign["status"] == "completed"
    assert campaign["targets"]["completed"] == 3
    assert campaign["targets"]["suppressed"] == 1


async def test_never_more_live_calls_than_the_project_limit(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app, live_calls=2)
        numbers = _numbers(5)
        campaign_id = await _create(app, agent_id, number_id, numbers)

        live: list[CarrierAction] = []
        most_live = 0
        for _ in range(10):
            before = len(_dials(app, caller_id))
            await _pass(workers=2)
            live += _dials(app, caller_id)[before:]
            most_live = max(most_live, len(live))
            if not live:
                break
            await _end(app, live.pop(0), "ringing", "no-answer")
        await _pass()
        targets = await _targets(app, campaign_id)
        campaign = (await app.api.get(f"/v1/campaigns/{campaign_id}")).json()["data"]

    assert most_live == 2
    dialled = Counter(a.params["to_number"] for a in _dials(app, caller_id))
    assert dialled == Counter(numbers)
    assert {t["status"] for t in targets.values()} == {"completed"}
    assert campaign["status"] == "completed"


async def test_a_targets_variables_reach_its_agent(monkeypatch, redis_env):
    from turncall.orchestrator import pipeline_factory

    prompts: list[str] = []
    async with run_app(
        Script(agent=("Hello there.",)),
        set_env=monkeypatch.setenv,
        set_attr=monkeypatch.setattr,
    ) as app:
        stub = pipeline_factory._create_llm_service

        def _llm(config: Any, *args: Any, **kwargs: Any) -> Any:
            prompts.append(config.system_prompt)
            return stub(config, *args, **kwargs)

        monkeypatch.setattr(pipeline_factory, "_create_llm_service", _llm)
        _, caller_id, number_id = await limited_project(app)
        agent_id = await app.create_agent(
            {**AGENT, "system_prompt": "You call {{name}}."}
        )
        [number] = _numbers(1)
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "one",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "targets": [
                    {
                        "number": number,
                        "timezone": "Europe/London",
                        "variables": {"name": "Ada", "tier": 2},
                    }
                ],
            },
        )
        assert r.status_code == 201, r.text
        campaign_id = r.json()["data"]["id"]

        await _pass()
        call = await answer_outbound_call(
            app.carrier, app.api, _dials(app, caller_id)[0]
        )
        await call.wait_for_agent_speech(1)
        await call.hang_up()
        await app.sink.wait_for("call.ended")
        await _pass()
        targets = await _targets(app, campaign_id)

    assert "You call Ada." in prompts
    assert targets[number]["variables"] == {"name": "Ada", "tier": "2"}
    assert targets[number]["timezone"] == "Europe/London"
    assert targets[number]["status"] == "completed"
    assert targets[number]["ended_reason"] == "customer_ended_call"


async def test_create_refuses_a_target_with_no_timezone_anywhere(
    monkeypatch, redis_env
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        first, second = _numbers(2)
        body = {
            "name": "x",
            "agent_id": agent_id,
            "from_number_id": number_id,
            "targets": [
                {"number": first, "timezone": "Asia/Tokyo"},
                {"number": second},
            ],
        }
        no_zone = await app.api.post("/v1/campaigns", json=body)
        bad_zone = await app.api.post(
            "/v1/campaigns", json={**body, "timezone": "EST+5"}
        )
        defaulted = await app.api.post(
            "/v1/campaigns", json={**body, "timezone": "America/Chicago"}
        )
        listed = (await app.api.get("/v1/campaigns")).json()

    assert no_zone.status_code == 422
    assert "no timezone" in no_zone.text
    assert bad_zone.status_code == 422
    assert "IANA" in bad_zone.text
    assert defaulted.status_code == 201, defaulted.text
    assert defaulted.json()["data"]["targets"]["pending"] == 2
    # Refused creates wrote nothing.
    assert listed["total"] == 1
    assert _dials(app, caller_id) == []


async def test_the_worker_wakes_for_a_new_campaign(monkeypatch, redis_env):
    """The running worker, not a hand-driven pass: idle on a long poll, it
    dials a campaign created after it went to sleep, because creating one
    pushes its id on the worker's list."""
    from turncall.campaigns import queue as campaign_queue
    from turncall.campaigns.worker import serve
    from turncall.storage.database import get_session_factory
    from turncall.storage.redis import get_redis

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        await get_redis().delete(campaign_queue.QUEUE_KEY)  # earlier tests' nudges
        worker = asyncio.create_task(
            serve(
                get_session_factory(),
                get_redis(),
                asyncio.Event(),
                poll_seconds=60,
                clock=_clock,
            )
        )
        try:
            await asyncio.sleep(0.3)  # its first pass found nothing; it waits
            [number] = _numbers(1)
            await _create(app, agent_id, number_id, [number])
            for _ in range(50):
                if dials := _dials(app, caller_id):
                    break
                await asyncio.sleep(0.1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    assert [d.params["to_number"] for d in dials] == [number]


async def test_a_deleted_projects_campaign_stops_dialling(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        await _create(app, agent_id, number_id, _numbers(2))
        r = await app.api.delete(f"/v1/projects/{app.project_id}")
        assert r.status_code == 200, r.text
        await _pass()

    assert _dials(app, caller_id) == []
