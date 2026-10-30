"""The derived half of a call's representation.

Everything here is worked out from the call row and its events, never stored
(but `ended_reason`, frozen on the row when retention deletes the events, #316).
The call read (`GET /v1/calls[/{id}]`) and the `call.ended` payload both merge
`derived_fields`, so the two views of one call cannot disagree — `ended_reason`
used to be computed only on the webhook side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from turncall.domain.call_state import infer_ended_reason, is_active_state
from turncall.domain.enums import CallEventType, CallStatus
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
    # `provider.failover` payloads in the order they happened (#222).
    failovers: tuple[dict[str, Any], ...] = ()
    # The skills the call resolved, `{id, skill, version, pinned}` in the agent's
    # order (a handoff's target's after, #427), and its `skill.loaded`
    # payloads in load order (#425).
    resolved_skills: tuple[dict[str, Any], ...] = ()
    skill_loads: tuple[dict[str, Any], ...] = ()
    # The call's `PROCEDURE_EVENTS`, `(event_type, payload)` in the order they
    # happened (#443).
    procedure_events: tuple[tuple[str, dict[str, Any]], ...] = ()


# Call-init's inline agent, stored raw on the call (ADR-0017). It carries the
# call's credentials — MCP headers, an inline `llm.api_key` — so it is read by
# the pipeline and `config_for_call()` and never shown (#276).
DYNAMIC_CONFIG = "dynamic_config"
# The skills the call resolved (#425): `[{id, skill, version, pinned}]`
# (`pinned` since #426; an entry without it was a live-following one), written by
# the pipeline's build and appended to by a handoff for the target agent's
# (#427), so the call stays explainable after a skill is published again.
# Shown as `skills` (`call_skills`), not here.
RESOLVED_SKILLS = "resolved_skills"
# Keys only TurnCall writes: an outbound dial's `metadata` may not set them.
RESERVED_METADATA = frozenset({DYNAMIC_CONFIG, RESOLVED_SKILLS})


def public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """A call's metadata as the read and `call.ended` show it."""
    return {k: v for k, v in (metadata or {}).items() if k not in RESERVED_METADATA}


def resolved_skills(metadata: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """The skills stored on a call row, each `{id, skill, version[, pinned]}`; anything
    else under the key is skipped rather than shown."""
    stored = (metadata or {}).get(RESOLVED_SKILLS)
    if not isinstance(stored, list):
        return ()
    return tuple(
        s
        for s in stored
        if isinstance(s, dict)
        and isinstance(s.get("skill"), str)
        and isinstance(s.get("version"), int)
    )


def call_skills(
    resolved: tuple[dict[str, Any], ...],
    loads: tuple[dict[str, Any], ...],
    *,
    events_kept: bool = True,
) -> list[dict[str, Any]]:
    """Every skill version the call resolved, in the agent's order (after a
    handoff, the target agent's next): its version, whether the agent pinned
    it (#426; an entry stored before pins existed reads false) and whether the
    call loaded it at any point. A skill unloaded later (#427) still reads loaded;
    its `skill.unloaded` event says when. Empty for a call with none.

    A load matches by name and version: two agents of one call may resolve
    two versions of a skill, and only the one loaded ran.

    `loaded` is null once retention deleted the call's events (#316): the
    loads they recorded are gone, and false would say the skill never ran.
    """
    loaded = {(load.get("skill"), load.get("version")) for load in loads}
    return [
        {
            "skill": s["skill"],
            "version": s["version"],
            "pinned": s.get("pinned") is True,
            "loaded": ((s["skill"], s["version"]) in loaded) if events_kept else None,
        }
        for s in resolved
    ]


# What a call's `procedures` are derived from (#443), read in one ordered
# stream: a summary reads the order across event types.
PROCEDURE_EVENTS = (CallEventType.PROCEDURE_STEP, CallEventType.PROCEDURE_REFUSED)


@dataclass(frozen=True)
class ProcedureWalk:
    """One procedure as its events tell it: every step reached, in order
    (a move back included), the kind of the last one and the moves refused."""

    name: str
    path: tuple[str, ...] = ()
    last_kind: str | None = None
    refused_moves: int = 0

    def summary(self) -> dict[str, Any]:
        ended_at = self.path[-1] if self.path else None
        return {
            "name": self.name,
            "path": list(self.path),
            "completed": self.last_kind == "end",
            "ended_at": ended_at,
            "escalated_at": ended_at if self.last_kind == "escalate" else None,
            "refused_moves": self.refused_moves,
        }


def _standing_at(path: tuple[str, ...], at: Any) -> tuple[str, ...]:
    """The path so far; an event's `from` opens it when it is the first heard
    of — the step the procedure started at moves nowhere to be recorded."""
    return path if path or not isinstance(at, str) else (at,)


def _walked(
    walk: ProcedureWalk, event_type: str, payload: dict[str, Any]
) -> ProcedureWalk:
    """`walk` with one more of its events read."""
    at, to = payload.get("from"), payload.get("to")
    if event_type == CallEventType.PROCEDURE_STEP and isinstance(to, str):
        return replace(
            walk,
            path=(*_standing_at(walk.path, at), to),
            last_kind=payload.get("kind"),
        )
    if event_type == CallEventType.PROCEDURE_REFUSED:
        return replace(
            walk,
            path=_standing_at(walk.path, at),
            refused_moves=walk.refused_moves + 1,
        )
    return walk


def call_procedures(
    events: tuple[tuple[str, dict[str, Any]], ...], *, events_kept: bool = True
) -> list[dict[str, Any]] | None:
    """What the call did of each procedure, in the order each first appears:
    `{name, path, completed, ended_at, escalated_at, refused_moves}`.
    `completed` when the last step reached is an `end` step; `escalated_at`
    that step when it is an `escalate` one. Empty for a call with none.

    Null once retention deleted the events (they go with the transcript,
    #316): an empty list would say the call followed no procedure.
    """
    if not events_kept:
        return None
    walks: dict[str, ProcedureWalk] = {}
    for event_type, payload in events:
        name = payload.get("procedure")
        if not isinstance(name, str):
            continue
        walks[name] = _walked(walks.get(name, ProcedureWalk(name)), event_type, payload)
    return [walk.summary() for walk in walks.values()]


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


def _ended_reason(call: Any, facts: CallFacts) -> str | None:
    # Frozen on the row when the call's events expired (#316): what it was
    # derived from is gone.
    frozen: str | None = getattr(call, "ended_reason", None)
    if frozen is not None:
        return frozen
    if call.ended_at is None and is_active_state(CallStatus(call.status)):
        return None
    return infer_ended_reason(
        call.status, set(facts.event_types), assistant_ended=facts.assistant_ended
    ).value


async def _ended_reason_facts(
    session: AsyncSession, call_ids: list[UUID]
) -> dict[UUID, CallFacts]:
    """The facts `ended_reason` is derived from, for a page of calls: the one
    loader the read and the retention janitor's freeze share, so the two
    cannot derive it from different facts."""
    event_types = await call_repo.list_event_types_by_call(session, call_ids)
    control_ended = await call_repo.list_control_ended_calls(session, call_ids)
    return {
        call_id: CallFacts(
            event_types=frozenset(event_types.get(call_id, ())),
            assistant_ended=call_id in control_ended,
        )
        for call_id in call_ids
    }


async def ended_reasons(
    session: AsyncSession, calls: list[CallRow]
) -> dict[UUID, str | None]:
    """Each call's `ended_reason` as the read derives it, and nothing else:
    what the retention janitor freezes before the events go (#316)."""
    if not calls:
        return {}
    facts = await _ended_reason_facts(session, [c.id for c in calls])
    return {c.id: _ended_reason(c, facts[c.id]) for c in calls}


def derived_fields(call: Any, facts: CallFacts) -> dict[str, Any]:
    """The derived fields of one call. Pure.

    `ended_reason` is null while the call is still live: "unknown" on a call
    that is talking would read as a verdict. `ended_at` is what says it is over,
    whatever the status. Agent fields are null for an inline agent (ADR-0017)
    and for an agent row deleted since.
    """
    ended_reason = _ended_reason(call, facts)
    return {
        "ended_reason": ended_reason,
        "agent_name": facts.agent_name,
        "agent_version": facts.agent_version,
        "latency_summary": latency_summary(facts.turn_metrics),
        "failovers": list(facts.failovers),
        "skills": call_skills(
            facts.resolved_skills,
            facts.skill_loads,
            events_kept=getattr(call, "events_expired_at", None) is None,
        ),
        "procedures": call_procedures(
            facts.procedure_events,
            events_kept=getattr(call, "transcript_expired_at", None) is None
            and getattr(call, "events_expired_at", None) is None,
        ),
    }


async def load_call_facts(
    session: AsyncSession, calls: list[CallRow]
) -> dict[UUID, CallFacts]:
    """Facts for a page of calls — a fixed number of queries, whatever its size."""
    if not calls:
        return {}
    call_ids = [c.id for c in calls]
    ended = await _ended_reason_facts(session, call_ids)
    turn_metrics = await call_repo.list_payloads_by_call(
        session, call_ids, CallEventType.METRICS_TURN
    )
    failovers = await call_repo.list_payloads_by_call(
        session, call_ids, CallEventType.PROVIDER_FAILOVER
    )
    resolved = {c.id: resolved_skills(c.metadata_json) for c in calls}
    skill_loads = (
        await call_repo.list_payloads_by_call(
            session, call_ids, CallEventType.SKILL_LOADED
        )
        if any(resolved.values())
        else {}
    )
    procedure_events = await call_repo.list_typed_payloads_by_call(
        session, call_ids, PROCEDURE_EVENTS
    )
    agent_ids = list({c.active_agent_id for c in calls if c.active_agent_id})
    agents = (
        await agent_repo.get_names_and_versions(session, agent_ids) if agent_ids else {}
    )
    facts = {}
    for call in calls:
        agent = agents.get(call.active_agent_id) if call.active_agent_id else None
        facts[call.id] = replace(
            ended[call.id],
            agent_name=agent[0] if agent else None,
            agent_version=agent[1] if agent else None,
            turn_metrics=tuple(turn_metrics.get(call.id, ())),
            failovers=tuple(failovers.get(call.id, ())),
            resolved_skills=resolved[call.id],
            skill_loads=tuple(skill_loads.get(call.id, ())),
            procedure_events=tuple(procedure_events.get(call.id, ())),
        )
    return facts


async def call_view_fields(
    session: AsyncSession, calls: list[CallRow]
) -> dict[UUID, dict[str, Any]]:
    """`derived_fields` for each call in a page, keyed by call id."""
    facts = await load_call_facts(session, calls)
    return {c.id: derived_fields(c, facts[c.id]) for c in calls}
