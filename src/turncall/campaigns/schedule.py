"""When a campaign target may be dialled: its calling window (#230).

The window is the last word on the earliest instant a target may be dialled:
whatever made a target due, a target outside its window waits for the next
opening.

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


def _instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)
