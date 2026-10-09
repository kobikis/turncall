"""When campaign targets are dialled, through the app and the worker's pass (#230).

The clock is the test's: the worker is stepped through a weekend that crosses
New York's fall-back, an hour per pass, against the fake carrier — so "never
outside the window" is a statement about the carrier's dial log, each dial
stamped with the instant of the pass that placed it.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.harness import run_app
from tests.harness.carrier import CarrierAction
from tests.integration.limits_support import SCRIPT, limited_project

pytestmark = pytest.mark.integration

WINDOW = {
    "start": "09:00",
    "end": "17:00",
    "days": ["mon", "tue", "wed", "thu", "fri"],
}


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _pass(clock: Clock) -> None:
    from turncall.campaigns.dialer import run_pass
    from turncall.storage.database import get_session_factory

    await run_pass(get_session_factory(), clock=clock)


def _dials(app: Any, from_number: str) -> list[CarrierAction]:
    """This test's dials: a pass serves every campaign in the database."""
    return [
        a
        for a in app.carrier.actions_of("dial")
        if a.params["from_number"] == from_number
    ]


async def _targets(app: Any, campaign_id: str) -> dict[str, dict]:
    r = await app.api.get(f"/v1/campaigns/{campaign_id}/targets")
    assert r.status_code == 200, r.text
    return {t["number"]: t for t in r.json()["data"]}


def _number() -> str:
    return f"+1555{secrets.randbelow(10**7):07d}"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# Each zone's first Monday 09:00 after Saturday 31 Oct 2026, in UTC. New York
# falls back on Sunday 1 Nov: its Monday opening is 14:00 UTC, an hour later
# than Saturday's offset would put it.
FIRST_OPENING = {
    "America/New_York": _utc(2026, 11, 2, 14),
    "Europe/London": _utc(2026, 11, 2, 9),
    "Australia/Sydney": _utc(2026, 11, 1, 22),
    "Asia/Tokyo": _utc(2026, 11, 2, 0),
}


async def test_no_target_is_dialled_outside_its_own_window(monkeypatch, redis_env):
    clock = Clock(_utc(2026, 10, 31, 12))  # Saturday
    zones = {_number(): zone for zone in FIRST_OPENING}
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "weekdays",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "calling_window": WINDOW,
                "targets": [
                    {"number": n, "timezone": zone} for n, zone in zones.items()
                ],
            },
        )
        assert r.status_code == 201, r.text
        campaign = r.json()["data"]
        campaign_id = campaign["id"]

        await _pass(clock)
        on_saturday = _dials(app, caller_id)
        waiting = await _targets(app, campaign_id)

        dialled_at: dict[str, list[datetime]] = {}
        while clock.now <= _utc(2026, 11, 2, 20):
            before = len(_dials(app, caller_id))
            await _pass(clock)
            for dial in _dials(app, caller_id)[before:]:
                dialled_at.setdefault(dial.params["to_number"], []).append(clock.now)
            clock.now += timedelta(hours=1)
        targets = await _targets(app, campaign_id)

    assert campaign["calling_window"] == {
        "start": "09:00:00",
        "end": "17:00:00",
        "days": WINDOW["days"],
    }
    # Nothing was due on a Saturday; each target waits for its own opening.
    assert on_saturday == []
    for number, zone in zones.items():
        assert waiting[number]["status"] == "pending"
        assert waiting[number]["attempts"] == 0
        assert _parse(waiting[number]["next_attempt_at"]) == FIRST_OPENING[zone]

    # Each dialled exactly once, on the first pass at its own opening.
    assert dialled_at == {n: [FIRST_OPENING[zone]] for n, zone in zones.items()}
    for number, zone in zones.items():
        [at] = dialled_at[number]
        local = at.astimezone(ZoneInfo(zone))
        assert local.isoweekday() <= 5
        assert 9 <= local.hour < 17
        assert targets[number]["status"] == "calling"
        assert targets[number]["attempts"] == 1


async def test_a_campaign_with_no_window_dials_at_any_hour(monkeypatch, redis_env):
    clock = Clock(_utc(2026, 11, 1, 7))  # Sunday 02:00 in New York
    number = _number()
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "any hour",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "timezone": "America/New_York",
                "targets": [{"number": number}],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["data"]["calling_window"] is None
        await _pass(clock)

    assert [d.params["to_number"] for d in _dials(app, caller_id)] == [number]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
