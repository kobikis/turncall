"""When a campaign target is next due: the window (#230), read in the target's
own zone on the day in question — never a fixed offset — and a retry (#231),
whose spacing the window then has the last word on."""

from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.campaigns import CreateCampaignRequest
from turncall.campaigns.schedule import CallingWindow, next_open, retry_at

WEEKDAYS_9_TO_5 = CallingWindow(
    start=time(9), end=time(17), days=frozenset({1, 2, 3, 4, 5})
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timezone", "now", "expected"),
    [
        # Saturday 31 Oct 2026, New York on EDT (-4); it falls back to EST (-5)
        # on Sunday 1 Nov, so Monday's 09:00 is 14:00 UTC — not Saturday's 13:00.
        ("America/New_York", _utc(2026, 10, 31, 12), _utc(2026, 11, 2, 14)),
        # London fell back on 25 Oct: Monday 09:00 GMT is 09:00 UTC.
        ("Europe/London", _utc(2026, 10, 31, 12), _utc(2026, 11, 2, 9)),
        # Sydney went forward to AEDT (+11) on 4 Oct: Monday 09:00 is Sunday UTC.
        ("Australia/Sydney", _utc(2026, 10, 31, 12), _utc(2026, 11, 1, 22)),
        # Tokyo keeps no DST.
        ("Asia/Tokyo", _utc(2026, 10, 31, 12), _utc(2026, 11, 2, 0)),
        # Friday 17:00 in New York closed the week: Monday, after the change.
        ("America/New_York", _utc(2026, 10, 30, 21), _utc(2026, 11, 2, 14)),
        # Spring forward in New York (Sunday 14 Mar 2027): Saturday is EST,
        # Monday's 09:00 is EDT, 13:00 UTC.
        ("America/New_York", _utc(2027, 3, 13, 12), _utc(2027, 3, 15, 13)),
    ],
)
def test_a_closed_window_opens_at_local_start_across_dst(timezone, now, expected):
    assert next_open(now, timezone, WEEKDAYS_9_TO_5) == expected


@pytest.mark.parametrize(
    ("timezone", "now"),
    [
        ("America/New_York", _utc(2026, 11, 2, 14)),  # 09:00 EST, opening instant
        ("America/New_York", _utc(2026, 11, 2, 21, 59)),  # 16:59 EST
        ("Asia/Tokyo", _utc(2026, 11, 2, 7, 59)),  # 16:59 JST
    ],
)
def test_inside_the_window_is_due_now(timezone, now):
    assert next_open(now, timezone, WEEKDAYS_9_TO_5) == now


def test_the_end_is_exclusive_and_the_next_day_opens():
    # 17:00 EST on Monday: closed; Tuesday 09:00 EST.
    assert next_open(
        _utc(2026, 11, 2, 22), "America/New_York", WEEKDAYS_9_TO_5
    ) == _utc(2026, 11, 3, 14)


def test_a_one_day_window_waits_a_week_once_it_has_closed():
    mondays = CallingWindow(start=time(9), end=time(17), days=frozenset({1}))
    # Monday 2 Nov, 18:00 in Tokyo: the next Monday.
    assert next_open(_utc(2026, 11, 2, 9), "Asia/Tokyo", mondays) == _utc(
        2026, 11, 9, 0
    )


def test_no_window_is_always_due():
    now = _utc(2026, 11, 2, 3)
    assert next_open(now, "America/New_York", None) == now


def test_a_start_in_the_spring_forward_gap_opens_just_after_it():
    # 02:30 does not exist in New York on Sunday 14 Mar 2027; it reads as
    # 03:30 EDT.
    window = CallingWindow(start=time(2, 30), end=time(17), days=frozenset({7}))
    assert next_open(_utc(2027, 3, 14, 5), "America/New_York", window) == _utc(
        2027, 3, 14, 7, 30
    )


def test_the_stored_form_round_trips_from_the_request():
    body = _body(calling_window={"start": "09:00", "end": "17:30", "days": ["mon"]})
    stored = body.calling_window.model_dump(mode="json")
    assert CallingWindow.from_json(stored) == CallingWindow(
        start=time(9), end=time(17, 30), days=frozenset({1})
    )
    # Every day, when none are named.
    every_day = _body(calling_window={"start": "09:00", "end": "17:00"})
    assert CallingWindow.from_json(
        every_day.calling_window.model_dump(mode="json")
    ).days == frozenset(range(1, 8))


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"start": "22:00", "end": "02:00"}, "before its end"),
        ({"start": "09:00", "end": "09:00"}, "before its end"),
        ({"start": "09:00+02:00", "end": "17:00"}, "give no offset"),
        ({"start": "09:00", "end": "17:00", "days": []}, "at least 1"),
        ({"start": "09:00", "end": "17:00", "days": ["monday"]}, "'mon'"),
        ({"start": "09:00", "end": "17:00", "days": ["mon", "mon"]}, "twice"),
        ({"start": "9am", "end": "17:00"}, "time"),
    ],
)
def test_create_refuses_a_window_that_is_not_one(window, message):
    with pytest.raises(ValidationError, match=message):
        _body(calling_window=window)


POLICY = {
    "customer_did_not_answer": {"max_attempts": 3, "spacing_seconds": 3600},
    "customer_busy": {"max_attempts": 2, "spacing_seconds": 600},
    "voicemail": {"max_attempts": 2, "spacing_seconds": 7200},
}


def _retry(reason: str | None, attempts: int, now: datetime, **kw: object):
    return retry_at(
        kw.get("policy", POLICY),
        ended_reason=reason,
        attempts=attempts,
        now=now,
        timezone="America/New_York",
        window=kw.get("window"),
    )


@pytest.mark.parametrize(
    ("reason", "attempts", "after"),
    [
        ("customer_did_not_answer", 1, 3600),
        ("customer_did_not_answer", 2, 3600),
        ("customer_busy", 1, 600),
        ("voicemail", 1, 7200),
    ],
)
def test_a_retryable_ending_is_due_again_after_its_spacing(reason, attempts, after):
    now = _utc(2026, 11, 2, 15)
    assert _retry(reason, attempts, now) == now + timedelta(seconds=after)


@pytest.mark.parametrize(
    ("reason", "attempts", "policy"),
    [
        ("customer_did_not_answer", 3, POLICY),  # its attempts are spent
        ("voicemail", 2, POLICY),
        # The latest ending's rule counts every dial: after a no-answer, a busy
        # second attempt has spent busy's two.
        ("customer_busy", 2, POLICY),
        ("customer_ended_call", 1, POLICY),  # reached: never again
        ("transferred", 1, POLICY),
        ("telephony_failed", 1, POLICY),
        (None, 1, POLICY),
        ("customer_did_not_answer", 1, None),  # no policy, no retries
        ("customer_did_not_answer", 1, {}),
    ],
)
def test_any_other_ending_is_final(reason, attempts, policy):
    assert _retry(reason, attempts, _utc(2026, 11, 2, 15), policy=policy) is None


def test_a_retry_that_lands_outside_the_window_waits_for_it():
    # 16:30 EST on Monday; an hour on is 17:30, past the window: Tuesday 09:00.
    now = _utc(2026, 11, 2, 21, 30)
    assert _retry("customer_did_not_answer", 1, now, window=WEEKDAYS_9_TO_5) == _utc(
        2026, 11, 3, 14
    )
    # Busy's ten minutes still fall inside it.
    assert _retry("customer_busy", 1, now, window=WEEKDAYS_9_TO_5) == now + (
        timedelta(minutes=10)
    )


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            {"customer_ended_call": {"max_attempts": 2, "spacing_seconds": 60}},
            "'voicemail'",
        ),
        (
            {"voicemail": {"max_attempts": 0, "spacing_seconds": 60}},
            "greater than or equal to 1",
        ),
        (
            {"voicemail": {"max_attempts": 11, "spacing_seconds": 60}},
            "less than or equal to 10",
        ),
        (
            {"voicemail": {"max_attempts": 2, "spacing_seconds": 5}},
            "greater than or equal to 60",
        ),
        ({"voicemail": {"max_attempts": 2}}, "spacing_seconds"),
        ({"voicemail": {"max_attempts": 2, "spacing_seconds": 60, "x": 1}}, "Extra"),
    ],
)
def test_create_refuses_a_policy_that_is_not_one(policy, message):
    with pytest.raises(ValidationError, match=message):
        _body(retry_policy=policy)


def _body(**fields: object) -> CreateCampaignRequest:
    return CreateCampaignRequest.model_validate(
        {
            "name": "x",
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "from_number_id": "00000000-0000-0000-0000-000000000002",
            "timezone": "America/New_York",
            "targets": [{"number": "+15550001111"}],
            **fields,
        }
    )
