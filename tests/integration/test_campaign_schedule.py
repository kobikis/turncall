"""When campaign targets are dialled, through the app and the worker's pass:
calling windows (#230) and retries by `ended_reason` (#231).

The clock is the test's: the worker is stepped through a weekend that crosses
New York's fall-back, an hour per pass, against the fake carrier — so "never
outside the window" is a statement about the carrier's dial log, each dial
stamped with the instant of the pass that placed it. Calls end the way Twilio
ends them, by status callbacks to the dial's own callback URL.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.harness import post_signed, run_app
from tests.harness.carrier import CarrierAction
from tests.integration.limits_support import SCRIPT, limited_project

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch: Any) -> None:
    """No calls-per-second floor (#233): these tests are about when a target
    may be dialled, so one pass dials every target that is due. Pace is
    test_campaigns.py's."""
    monkeypatch.setenv("CAMPAIGN_DEFAULT_CALLS_PER_SECOND", "0")


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


async def test_a_deferred_target_gives_its_dial_reservation_back(
    monkeypatch, redis_env
):
    """Windows and pacing compose (#230, #233): the pass reserves a dial's
    live-call slot and calls-per-second place before it claims, and a target
    the window defers dials nothing under them — so they go back, and the next
    due target dials in the same pass. Held, one live call and one call a
    second would leave the open target waiting behind three closed ones."""
    from uuid import UUID

    from turncall.services import limits
    from turncall.storage.redis import get_redis

    clock = Clock(_utc(2026, 11, 2, 10))  # Monday: 10:00 in London only
    closed = {
        _number(): z for z in ("America/New_York", "Asia/Tokyo", "Australia/Sydney")
    }
    london = _number()
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(
            app, live_calls=1, outbound_calls_per_second=1
        )
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "london first",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "calling_window": WINDOW,
                "targets": [
                    *({"number": n, "timezone": z} for n, z in closed.items()),
                    {"number": london, "timezone": "Europe/London"},
                ],
            },
        )
        assert r.status_code == 201, r.text
        campaign_id = r.json()["data"]["id"]

        await _pass(clock)
        dialled = [d.params["to_number"] for d in _dials(app, caller_id)]
        targets = await _targets(app, campaign_id)
        held = await get_redis().zrange(
            limits._live_calls_key(UUID(app.project_id)), 0, -1
        )

    assert dialled == [london]
    assert targets[london]["status"] == "calling"
    # The only slot held is the London call's own.
    assert [h.decode() if isinstance(h, bytes) else h for h in held] == [
        targets[london]["call_id"]
    ]
    for number, zone in closed.items():
        assert targets[number]["status"] == "pending"
        assert targets[number]["attempts"] == 0
        assert _parse(targets[number]["next_attempt_at"]) > clock.now, zone


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


POLICY = {
    "customer_did_not_answer": {"max_attempts": 3, "spacing_seconds": 3600},
    "customer_busy": {"max_attempts": 2, "spacing_seconds": 600},
    "voicemail": {"max_attempts": 2, "spacing_seconds": 1800},
}
NO_ANSWER = ("ringing", "no-answer")
BUSY = ("busy",)
ANSWERED = ("ringing", "completed")
VOICEMAIL = ("voicemail",)  # the detector's event, then an answered call's end


async def test_retries_follow_the_policy_per_ended_reason(monkeypatch, redis_env):
    t0 = _utc(2026, 11, 2, 15)  # Monday, 10:00 in New York
    clock = Clock(t0)
    # Each target's endings, one per dial, in order.
    endings = {
        "no_answer": [NO_ANSWER, NO_ANSWER, NO_ANSWER],
        "busy": [BUSY, BUSY],
        "voicemail": [VOICEMAIL, VOICEMAIL],
        "reached": [NO_ANSWER, ANSWERED],
    }
    numbers = {name: _number() for name in endings}
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "retries",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "timezone": "America/New_York",
                "calling_window": WINDOW,
                "retry_policy": POLICY,
                "targets": [{"number": n} for n in numbers.values()],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["data"]["retry_policy"] == POLICY
        campaign_id = r.json()["data"]["id"]
        name_of = {n: name for name, n in numbers.items()}

        dialled_at: dict[str, list[datetime]] = {}
        after_first: dict[str, dict] = {}
        # Every five minutes for three hours: a pass dials what is due, every
        # new call ends its target's next way, and the next pass settles it.
        while clock.now <= t0 + timedelta(hours=3):
            before = len(_dials(app, caller_id))
            await _pass(clock)
            for dial in _dials(app, caller_id)[before:]:
                name = name_of[dial.params["to_number"]]
                dialled_at.setdefault(name, []).append(clock.now)
                await _end(app, dial, *endings[name][len(dialled_at[name]) - 1])
            await _pass(clock)
            if clock.now == t0:
                after_first = await _targets(app, campaign_id)
            clock.now += timedelta(minutes=5)
        targets = await _targets(app, campaign_id)
        campaign = (await app.api.get(f"/v1/campaigns/{campaign_id}")).json()["data"]

    minutes = {
        name: [int((at - t0).total_seconds() // 60) for at in ats]
        for name, ats in dialled_at.items()
    }
    # Each ending's own spacing, measured from the pass that saw it end.
    assert minutes == {
        "no_answer": [0, 60, 120],
        "busy": [0, 10],
        "voicemail": [0, 30],
        "reached": [0, 60],
    }
    first = {name: after_first[n] for name, n in numbers.items()}
    assert {name: t["status"] for name, t in first.items()} == dict.fromkeys(
        endings, "pending"
    )
    assert first["busy"]["ended_reason"] == "customer_busy"
    assert _parse(first["busy"]["next_attempt_at"]) == t0 + timedelta(minutes=10)
    assert first["voicemail"]["ended_reason"] == "voicemail"

    final = {name: targets[n] for name, n in numbers.items()}
    assert {
        name: (t["status"], t["attempts"], t["ended_reason"])
        for name, t in final.items()
    } == {
        "no_answer": ("completed", 3, "customer_did_not_answer"),
        "busy": ("completed", 2, "customer_busy"),
        "voicemail": ("completed", 2, "voicemail"),
        # Reached on the second dial: never called again, attempts left or not.
        "reached": ("completed", 2, "customer_ended_call"),
    }
    assert campaign["status"] == "completed"


async def test_a_retry_outside_the_window_waits_for_it(monkeypatch, redis_env):
    t0 = _utc(2026, 11, 2, 21, 30)  # Monday, 16:30 in New York
    clock = Clock(t0)
    number = _number()
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        r = await app.api.post(
            "/v1/campaigns",
            json={
                "name": "late",
                "agent_id": agent_id,
                "from_number_id": number_id,
                "calling_window": WINDOW,
                "retry_policy": POLICY,
                "targets": [{"number": number, "timezone": "America/New_York"}],
            },
        )
        assert r.status_code == 201, r.text
        campaign_id = r.json()["data"]["id"]

        await _pass(clock)
        [first] = _dials(app, caller_id)
        await _end(app, first, *NO_ANSWER)
        await _pass(clock)
        waiting = (await _targets(app, campaign_id))[number]

        dialled_at: list[datetime] = []
        while clock.now <= _utc(2026, 11, 3, 16):
            clock.now += timedelta(minutes=30)
            before = len(_dials(app, caller_id))
            await _pass(clock)
            dialled_at += [clock.now] * (len(_dials(app, caller_id)) - before)

    # An hour on is 17:30, past the window: Tuesday's 09:00, 14:00 UTC.
    assert waiting["status"] == "pending"
    assert waiting["attempts"] == 1
    assert _parse(waiting["next_attempt_at"]) == _utc(2026, 11, 3, 14)
    assert dialled_at == [_utc(2026, 11, 3, 14)]


async def _end(app: Any, dial: CarrierAction, *statuses: str) -> None:
    """Twilio reporting a dialled call's progress, ending in its last status.
    `voicemail` records what the pipeline's detector records, then the call
    ends as an answered one does."""
    if statuses == VOICEMAIL:
        await _detect_voicemail(dial.params["call_sid"])
        statuses = ANSWERED
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


async def _detect_voicemail(call_sid: str) -> None:
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import call_repo

    async with get_session_factory()() as session:
        call = await call_repo.get_call_by_provider_sid(session, call_sid)
        await call_repo.create_call_event(
            session, call_id=call.id, event_type="voicemail.detected"
        )
        await session.commit()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
