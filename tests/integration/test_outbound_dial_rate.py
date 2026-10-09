"""Outbound calls per second, per number (#225).

Carriers cap how fast one number may dial and flag a number that dials faster.
The cap is per number, counted in Redis across servers: dial past it and the
excess is a 429 the carrier never sees, with how long to wait.
"""

import asyncio

import pytest

from tests.harness import run_app
from tests.harness.second_instance import second_instance
from tests.integration.limits_support import SCRIPT, dial_body, limited_project

pytestmark = pytest.mark.integration


async def test_dialling_faster_than_the_cap_is_429_for_the_excess(
    monkeypatch, redis_env
):
    async with (
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        second_instance() as other,
    ):
        other.headers["Authorization"] = app.api.headers["Authorization"]
        agent_id, _, number_id = await limited_project(app, outbound_calls_per_second=1)
        dial = dial_body(agent_id, number_id)

        first = await other.post("/v1/calls/outbound", json=dial)
        excess = await app.api.post("/v1/calls/outbound", json=dial)
        dialled_before_waiting = len(app.carrier.actions_of("dial"))
        wait_ms = excess.json()["details"]["retry_after_ms"]
        await asyncio.sleep(wait_ms / 1000 + 0.05)
        after = await app.api.post("/v1/calls/outbound", json=dial)
        calls = (await app.api.get("/v1/calls")).json()["data"]

    assert first.status_code == 201, first.text
    assert excess.status_code == 429
    assert excess.json()["code"] == "rate_limited"
    details = excess.json()["details"]
    assert details["limit"] == "outbound_calls_per_second"
    assert details["max"] == 1
    assert 0 < wait_ms <= 1000
    assert dialled_before_waiting == 0  # the excess never reached the carrier
    assert after.status_code == 201, after.text
    assert len(app.carrier.actions_of("dial")) == 1
    # The refused dial wrote no call row and held no live-call slot.
    assert len(calls) == 2


async def test_unset_dials_as_fast_as_asked(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, number_id = await limited_project(app)
        dial = dial_body(agent_id, number_id)
        answers = [
            await app.api.post("/v1/calls/outbound", json=dial) for _ in range(5)
        ]

    assert [a.status_code for a in answers] == [201] * 5
    assert len(app.carrier.actions_of("dial")) == 5


async def test_the_cap_is_per_number(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, _, first_number = await limited_project(
            app, outbound_calls_per_second=1
        )
        await app.bind_number(agent_id)
        numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
        second_number = next(n["id"] for n in numbers if n["id"] != first_number)

        one = await app.api.post(
            "/v1/calls/outbound", json=dial_body(agent_id, first_number)
        )
        other = await app.api.post(
            "/v1/calls/outbound", json=dial_body(agent_id, second_number)
        )

    assert (one.status_code, other.status_code) == (201, 201)
