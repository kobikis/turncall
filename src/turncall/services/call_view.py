"""The derived half of a call's representation.

Everything here is worked out from the call row and its events, never stored.
The call read (`GET /v1/calls[/{id}]`) and the `call.ended` payload both merge
`derived_fields`, so the two views of one call cannot disagree — `ended_reason`
used to be computed only on the webhook side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from turncall.domain.call_state import infer_ended_reason, is_active_state
from turncall.domain.enums import CallStatus
from turncall.storage.repositories import agent_repo, call_repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.storage.models import CallRow


@dataclass(frozen=True)
class CallFacts:
    """What the view needs beyond the call row itself."""

    event_types: frozenset[str] = frozenset()
    assistant_ended: bool = False
    agent_name: str | None = None
    agent_version: int | None = None
    # `metrics.turn` payloads in turn order (#176).
    turn_metrics: tuple[dict[str, Any], ...] = ()


_STAGES = ("stt", "llm", "tts")


def _nearest_rank(values: list[int], pct: float) -> int:
    """Nearest-rank percentile: always a value that actually happened."""
    ordered = sorted(values)
    return ordered[max(math.ceil(pct * len(ordered)) - 1, 0)]


def _spread(values: list[int]) -> dict[str, int]:
    return {
        "p50_ms": _nearest_rank(values, 0.50),
        "p95_ms": _nearest_rank(values, 0.95),
        "count": len(values),
    }


def latency_summary(turns: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    """The latency summary of a call, derived from its turn metrics.

    Null with no measured turns — zeros would read as "instant". A stage no
    turn reported (TTS on an S2S call) is absent rather than zero.
    """
    if not turns:
        return None
    stages = {}
    for stage in _STAGES:
        values = [t["stages"][stage]["ttfb_ms"] for t in turns if stage in t["stages"]]
        if values:
            stages[stage] = _spread(values)
    slowest = max(turns, key=lambda t: t["user_to_bot_ms"])
    return {
        "turns": len(turns),
        "user_to_bot": _spread([t["user_to_bot_ms"] for t in turns]),
        "stages": stages,
        "slowest_turn": {
            "turn": slowest["turn"],
            "user_to_bot_ms": slowest["user_to_bot_ms"],
        },
    }


def derived_fields(call: Any, facts: CallFacts) -> dict[str, Any]:
    """The derived fields of one call. Pure.

    `ended_reason` is null while the call is still live: "unknown" on a call
    that is talking would read as a verdict. `ended_at` is what says it is over,
    whatever the status. Agent fields are null for an inline agent (ADR-0017)
    and for an agent row deleted since.
    """
    live = call.ended_at is None and is_active_state(CallStatus(call.status))
    ended_reason = (
        None
        if live
        else infer_ended_reason(
            call.status, set(facts.event_types), assistant_ended=facts.assistant_ended
        ).value
    )
    return {
        "ended_reason": ended_reason,
        "agent_name": facts.agent_name,
        "agent_version": facts.agent_version,
        "latency_summary": latency_summary(facts.turn_metrics),
    }


async def load_call_facts(
    session: AsyncSession, calls: list[CallRow]
) -> dict[UUID, CallFacts]:
    """Facts for a page of calls — a fixed number of queries, whatever its size."""
    if not calls:
        return {}
    call_ids = [c.id for c in calls]
    event_types = await call_repo.list_event_types_by_call(session, call_ids)
    control_ended = await call_repo.list_control_ended_calls(session, call_ids)
    turn_metrics = await call_repo.list_turn_metrics_by_call(session, call_ids)
    agent_ids = list({c.active_agent_id for c in calls if c.active_agent_id})
    agents = (
        await agent_repo.get_names_and_versions(session, agent_ids) if agent_ids else {}
    )
    facts = {}
    for call in calls:
        agent = agents.get(call.active_agent_id) if call.active_agent_id else None
        facts[call.id] = CallFacts(
            event_types=frozenset(event_types.get(call.id, ())),
            assistant_ended=call.id in control_ended,
            agent_name=agent[0] if agent else None,
            agent_version=agent[1] if agent else None,
            turn_metrics=tuple(turn_metrics.get(call.id, ())),
        )
    return facts


async def call_view_fields(
    session: AsyncSession, calls: list[CallRow]
) -> dict[UUID, dict[str, Any]]:
    """`derived_fields` for each call in a page, keyed by call id."""
    facts = await load_call_facts(session, calls)
    return {c.id: derived_fields(c, facts[c.id]) for c in calls}
