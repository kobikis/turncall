"""When a campaign target is next due: calling windows (#230) and retries (#231).

Both answer one question — the earliest instant a target may be dialled — so
they compose by construction: a retry's spacing gives the earliest instant,
and the window moves it to the next opening when it lands outside. A retry
due at 17:30 against a 09:00-17:00 window waits for 09:00 the next allowed day.

A window is wall-clock time in the **target's own** IANA zone, resolved per
day, never a fixed offset: "09:00 in New York" is 13:00 UTC in October and
14:00 UTC in November, and an offset computed once on Saturday would call at
08:00 on Monday. Everything here is pure; the dialler passes the clock in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class CallingWindow:
    """Local hours [start, end) on the given ISO weekdays (1 = Monday)."""

    start: time
    end: time
    days: frozenset[int]

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> CallingWindow | None:
        """The stored form (`{start: "09:00:00", end: ..., days: ["mon", ...]}`)."""
        if not data:
            return None
        return cls(
            start=time.fromisoformat(data["start"]),
            end=time.fromisoformat(data["end"]),
            days=frozenset(WEEKDAYS.index(d) + 1 for d in data["days"]),
        )


def next_open(now: datetime, timezone: str, window: CallingWindow | None) -> datetime:
    """`now` when the target may be called now, else when its window next opens.

    A start that falls in a spring-forward gap reads with the pre-gap offset
    (zoneinfo's fold=0), which lands just after the gap — later, never earlier.
    """
    if window is None:
        return now
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    # Seven days always holds an allowed day; the eighth covers today's window
    # having already closed when today is the only allowed day.
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in window.days:
            continue
        opens = _instant(day, window.start, zone)
        if now < opens:
            return opens
        if now < _instant(day, window.end, zone):
            return now
    raise AssertionError("a calling window with no allowed day")  # pragma: no cover


def retry_at(
    policy: Mapping[str, Any] | None,
    *,
    ended_reason: str | None,
    attempts: int,
    now: datetime,
    timezone: str,
    window: CallingWindow | None,
) -> datetime | None:
    """When to dial a target again after a call ended this way, or None.

    `attempts` counts the dials placed so far, the one that just ended
    included; a rule's `max_attempts` counts them the same way. An outcome the
    policy names no rule for is terminal — a person who answered is not
    called back because some earlier attempt went unanswered.
    """
    rule = (policy or {}).get(ended_reason or "")
    if rule is None or attempts >= rule["max_attempts"]:
        return None
    earliest = now + timedelta(seconds=rule["spacing_seconds"])
    return next_open(earliest, timezone, window)


def _instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)
