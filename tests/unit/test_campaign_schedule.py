"""When a campaign target may be dialled (#230): the window, read in the
target's own zone on the day in question — never a fixed offset."""

from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.campaigns import CreateCampaignRequest
from turncall.campaigns.schedule import CallingWindow, next_open

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
