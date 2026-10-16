"""A call's conversation as it was said, held in this process's memory (#313).

Redaction (ADR-0024) rewrites the transcript before it is stored, and must
never reach what the call itself uses. Two things read the conversation on the
call's behalf after the words were spoken: the warm-transfer briefing (the
operator needs the caller's name and number) and post-call analysis with its
takeaways (they extract that number). Both read it here, raw; only their
outputs are redacted on the way out.

The transcript taps (`orchestrator/observability.py`) add each utterance the
moment they log it, so the lines are in the order they were said. The process
running the call holds them until `KEEP_AFTER_CALL_SECONDS` after its pipeline
stops — long enough for a warm transfer's operator to answer and for post-call
processing to run — then forgets them. Nothing here is stored or sent.

A process that never ran the call (another replica took the carrier's
callback, or the server restarted) has nothing, and its readers fall back to
the stored transcript: identical with redaction off, redacted with it on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

# The transfer intent lives this long too (services/transfer.py), so a
# briefing resolved while the intent is still parked finds the conversation.
KEEP_AFTER_CALL_SECONDS = 300.0


@dataclass(frozen=True)
class Line:
    """One utterance. `role` is "assistant" for the agent, anything else (the
    caller's transport id, "customer") for the caller — as on the event."""

    role: str
    text: str

    @property
    def is_agent(self) -> bool:
        return self.role == "assistant"


_LINES: dict[UUID, list[Line]] = {}
_FORGET: dict[UUID, asyncio.TimerHandle] = {}


def said(call_id: UUID, role: str, text: str) -> None:
    """Add an utterance to the call's conversation."""
    _LINES.setdefault(call_id, []).append(Line(role, text))


def lines(call_id: UUID) -> list[Line] | None:
    """The call's conversation, or None when this process does not hold it."""
    held = _LINES.get(call_id)
    return list(held) if held is not None else None


def release(call_id: UUID, *, after: float = KEEP_AFTER_CALL_SECONDS) -> None:
    """The call's pipeline has stopped: forget its conversation `after`
    seconds from now."""
    if (pending := _FORGET.pop(call_id, None)) is not None:
        pending.cancel()
    if after <= 0:
        _LINES.pop(call_id, None)
        return
    _FORGET[call_id] = asyncio.get_running_loop().call_later(after, _forget, call_id)


def _forget(call_id: UUID) -> None:
    _FORGET.pop(call_id, None)
    _LINES.pop(call_id, None)
