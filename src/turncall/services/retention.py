"""Retention (#316, ADR-0024): how long a call's data is kept, and the janitor
that deletes it after.

A project sets it per kind (`PUT /v1/projects/{id}/retention`), in days from
the call's end; an agent's `retention` overrides any kind for its calls. Null
everywhere keeps everything, as before retention existed. Three kinds:

- **recording**: the audio in object storage (both keys a call's recording
  can have, as the project purge sweeps them) and `calls.recording_url`.
- **transcript**: what was said and what was made from it. The
  `TRANSCRIPT_EVENTS` on the call's event log (the lines, the tool calls with
  their arguments and results, messages injected into the conversation), the
  call's `tool_invocations` rows, and the call's `analysis_json` (summary,
  structured data, takeaways).
- **events**: every other row on the call's event log. `ended_reason`, derived
  from them, is frozen onto the call row first, so the read still says how the
  call ended; `latency_summary` and `failovers` go with them.

The call row is never deleted: who called whom, when, for how long, its
status and metadata stay, with `*_expired_at` saying what went and when.

The agent whose override applies is the call's `active_agent_id` (the last
one, after a handoff), read from that agent's **live version** (the
published one, else the newest), so changing an agent's retention and
publishing covers the calls of its earlier versions too, as a project change
covers the project's earlier calls. An inline agent (ADR-0017) has no stored
version: its calls take the project's.

The janitor touches `calls`, `call_events`, `tool_invocations` and recording
objects, nothing else; the audit log (#317) keeps its own retention.
`tests/integration/test_retention_janitor.py` holds that against the
statements it runs.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.adapters.storage import ObjectStorageAdapter
from turncall.domain.enums import AgentState, CallEventType
from turncall.domain.models import RetentionConfig
from turncall.services.recording_storage import recording_storage_key
from turncall.storage.models import AgentRow, CallRow, ProjectRow
from turncall.storage.repositories import call_repo, tool_invocation_repo

Clock = Callable[[], datetime]

# The kinds, as the janitor names them; `Retention`'s fields are `<kind>s_days`
# (`events_days` for events).
RECORDING, TRANSCRIPT, EVENTS = "recording", "transcript", "events"
KINDS = (RECORDING, TRANSCRIPT, EVENTS)

# Call events that carry the conversation, and go with the transcript.
TRANSCRIPT_EVENTS = frozenset(
    {
        CallEventType.TRANSCRIPT_PARTIAL.value,
        CallEventType.TRANSCRIPT_FINAL.value,
        CallEventType.TOOL_CALLED.value,
        CallEventType.TOOL_RESULT.value,
        CallEventType.CONTEXT_INJECTED.value,
    }
)

SWEEP_INTERVAL_SECONDS = 3600  # retention is in days; an hour late is in it
BATCH = 200  # calls per transaction, locked while their data is deleted


@dataclass(frozen=True)
class Retention:
    """Days from a call's end each kind is kept; None = for good."""

    recordings_days: int | None = None
    transcripts_days: int | None = None
    events_days: int | None = None

    def days(self, kind: str) -> int | None:
        return {
            RECORDING: self.recordings_days,
            TRANSCRIPT: self.transcripts_days,
            EVENTS: self.events_days,
        }[kind]

    def over(self, base: Retention) -> Retention:
        """This override on `base`: each kind set here wins."""
        return Retention(
            **{
                f.name: getattr(self, f.name)
                if getattr(self, f.name) is not None
                else getattr(base, f.name)
                for f in fields(self)
            }
        )

    @property
    def empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def as_payload(self) -> dict[str, int | None]:
        """As `call.ended` states it."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def of_project(row: ProjectRow | None) -> Retention:
    if row is None:
        return Retention()
    return Retention(
        recordings_days=row.retention_recordings_days,
        transcripts_days=row.retention_transcripts_days,
        events_days=row.retention_events_days,
    )


def of_agent_config(config_blob: dict[str, Any] | None) -> Retention:
    """An agent's override, from its stored config. A block a later build
    would refuse is no override (logged), never a failed sweep."""
    try:
        config = RetentionConfig.model_validate(
            (config_blob or {}).get("retention") or {}
        )
    except ValidationError:
        logger.warning("agent_retention_unreadable")
        return Retention()
    return Retention(
        recordings_days=config.recordings_days,
        transcripts_days=config.transcripts_days,
        events_days=config.events_days,
    )


def _live_versions(agents: Iterable[Any]) -> dict[tuple[UUID, str], Any]:
    """Each agent name's live version: the published one, else the newest."""
    live: dict[tuple[UUID, str], Any] = {}
    for agent in agents:
        key = (agent.project_id, agent.name)
        held = live.get(key)
        rank = (agent.state == AgentState.PUBLISHED.value, agent.version)
        if held is None or rank > (
            held.state == AgentState.PUBLISHED.value,
            held.version,
        ):
            live[key] = agent
    return live


def _overrides(agents: list[Any]) -> dict[UUID, Retention]:
    """Every version's id → its name's live override, where it has one."""
    live = _live_versions(agents)
    out = {}
    for agent in agents:
        override = of_agent_config(live[(agent.project_id, agent.name)].config_blob)
        if not override.empty:
            out[agent.id] = override
    return out


async def for_call(session: AsyncSession, call: CallRow) -> Retention:
    """The retention that applies to `call` now: what `call.ended` states."""
    project = await session.get(ProjectRow, call.project_id)
    base = of_project(project)
    if call.active_agent_id is None:
        return base
    name = select(AgentRow.name).where(AgentRow.id == call.active_agent_id)
    agents = (
        await session.execute(
            select(AgentRow).where(
                AgentRow.project_id == call.project_id,
                AgentRow.name == name.scalar_subquery(),
            )
        )
    ).scalars()
    override = _overrides(list(agents)).get(call.active_agent_id)
    return override.over(base) if override else base


# --- The janitor ----------------------------------------------------------


@dataclass(frozen=True)
class _Scope:
    """Which of a project's calls one cutoff covers: those of `only` agents,
    or every other call (no agent, or an agent not in `exclude`)."""

    only: tuple[UUID, ...] | None = None
    exclude: tuple[UUID, ...] = ()

    def where(self) -> Any:
        if self.only is not None:
            return CallRow.active_agent_id.in_(self.only)
        if not self.exclude:
            return None
        return or_(
            CallRow.active_agent_id.is_(None),
            CallRow.active_agent_id.not_in(self.exclude),
        )


@dataclass(frozen=True)
class _Plan:
    project_id: UUID
    base: Retention
    overrides: dict[UUID, Retention] = field(default_factory=dict)

    def cutoffs(self, kind: str) -> list[tuple[int, _Scope]]:
        """(days, scope) for each distinct retention of `kind` in the project."""
        by_days: dict[int, list[UUID]] = defaultdict(list)
        for agent_id, override in self.overrides.items():
            days = override.days(kind)
            if days is not None:
                by_days[days].append(agent_id)
        out = [(days, _Scope(only=tuple(ids))) for days, ids in by_days.items()]
        if (days := self.base.days(kind)) is not None:
            overridden = tuple(i for ids in by_days.values() for i in ids)
            out.append((days, _Scope(exclude=overridden)))
        return out


@dataclass
class Swept:
    """Calls whose data of each kind the janitor deleted."""

    recording: int = 0
    transcript: int = 0
    events: int = 0

    @property
    def total(self) -> int:
        return self.recording + self.transcript + self.events


def _days_set(*columns: Any) -> Any:
    return or_(*(c.is_not(None) for c in columns))


async def _plans(session: AsyncSession) -> list[_Plan]:
    """Every live project with a retention set, itself or on an agent. Unset
    everywhere, a project is not here and the janitor never reads its calls."""
    days = AgentRow.config_blob["retention"]
    overriding = select(AgentRow.project_id, AgentRow.name).where(
        _days_set(
            days["recordings_days"].astext,
            days["transcripts_days"].astext,
            days["events_days"].astext,
        )
    )
    # All versions of an agent any version of which overrides: the live
    # version's says what applies to every version's calls.
    agents = (
        (
            await session.execute(
                select(AgentRow).where(
                    tuple_(AgentRow.project_id, AgentRow.name).in_(overriding)
                )
            )
        )
        .scalars()
        .all()
    )
    overrides = _overrides(list(agents))
    by_project: dict[UUID, dict[UUID, Retention]] = defaultdict(dict)
    for agent in agents:
        if agent.id in overrides:
            by_project[agent.project_id][agent.id] = overrides[agent.id]
    projects = (
        (
            await session.execute(
                select(ProjectRow).where(
                    ProjectRow.deleted_at.is_(None),
                    or_(
                        _days_set(
                            ProjectRow.retention_recordings_days,
                            ProjectRow.retention_transcripts_days,
                            ProjectRow.retention_events_days,
                        ),
                        ProjectRow.id.in_(list(by_project)),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        _Plan(p.id, of_project(p), by_project.get(p.id, {}))
        for p in projects
        if not of_project(p).empty or by_project.get(p.id)
    ]


_EXPIRED_AT = {
    RECORDING: CallRow.recording_expired_at,
    TRANSCRIPT: CallRow.transcript_expired_at,
    EVENTS: CallRow.events_expired_at,
}


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    storage: ObjectStorageAdapter,
    *,
    now: datetime,
) -> Swept:
    """Delete every call's data that is past its retention at `now`. Safe on
    every replica at once: each batch of calls is locked, others skipped."""
    async with session_factory() as session:
        plans = await _plans(session)
    swept = Swept()
    for plan in plans:
        for kind in KINDS:
            for days, scope in plan.cutoffs(kind):
                count = await _sweep_cutoff(
                    session_factory,
                    storage,
                    plan.project_id,
                    kind,
                    scope,
                    cutoff=now - timedelta(days=days),
                    now=now,
                )
                setattr(swept, kind, getattr(swept, kind) + count)
    return swept


async def _sweep_cutoff(
    session_factory: async_sessionmaker[AsyncSession],
    storage: ObjectStorageAdapter,
    project_id: UUID,
    kind: str,
    scope: _Scope,
    *,
    cutoff: datetime,
    now: datetime,
) -> int:
    """`kind` of the project's calls in `scope` that ended before `cutoff`, a
    batch at a time, in creation order (a keyset, so a call whose recording
    could not be deleted is retried next sweep, not next batch)."""
    expired_at = _EXPIRED_AT[kind]
    conditions = [
        CallRow.project_id == project_id,
        expired_at.is_(None),
        # created_at first: the partial index's range. A call created within
        # the window cannot have ended before it.
        CallRow.created_at < cutoff,
        # A call that never recorded an end (a crash) counts from its start.
        func.coalesce(CallRow.ended_at, CallRow.created_at) < cutoff,
    ]
    if (where := scope.where()) is not None:
        conditions.append(where)
    count = 0
    after: tuple[datetime, UUID] | None = None
    while True:
        query = select(CallRow).where(*conditions)
        if after is not None:
            query = query.where(tuple_(CallRow.created_at, CallRow.id) > after)
        query = (
            query.order_by(CallRow.created_at, CallRow.id)
            .limit(BATCH)
            .with_for_update(skip_locked=True)
        )
        async with session_factory() as session:
            calls = list((await session.execute(query)).scalars().all())
            if not calls:
                return count
            after = (calls[-1].created_at, calls[-1].id)
            count += await _EXPIRE[kind](session, storage, calls, now)
            await session.commit()
        if len(calls) < BATCH:
            return count


async def _expire_recordings(
    session: AsyncSession,
    storage: ObjectStorageAdapter,
    calls: list[CallRow],
    now: datetime,
) -> int:
    """Delete each call's recording objects, then mark it. Objects first: a
    commit that fails leaves the call to the next sweep, which deletes again
    (idempotent), where the other order could orphan an object for good. A
    call whose object could not be deleted is left unmarked and retried."""
    done = 0
    for call in calls:
        if call.recording_url is not None or call.recording_status != "none":
            # The recorder's key and Twilio's, as the project purge sweeps
            # them (#168): the URL can hide the first.
            keys = {
                f"recordings/{call.id}.wav",
                recording_storage_key(call.recording_url),
            }
            try:
                for key in sorted(k for k in keys if k):
                    await storage.delete(key)
            except Exception as exc:
                logger.warning(
                    "retention_recording_delete_failed call={call_id}: {err}",
                    call_id=str(call.id),
                    err=exc,
                )
                continue
        call.recording_url = None
        call.recording_expired_at = now
        done += 1
    return done


async def _expire_transcripts(
    session: AsyncSession,
    _storage: ObjectStorageAdapter,
    calls: list[CallRow],
    now: datetime,
) -> int:
    ids = [c.id for c in calls]
    await call_repo.delete_call_events(session, ids, types=TRANSCRIPT_EVENTS)
    await tool_invocation_repo.delete_for_calls(session, ids)
    await session.execute(
        update(CallRow)
        .where(CallRow.id.in_(ids))
        .values(analysis_json=None, transcript_expired_at=now)
        .execution_options(synchronize_session=False)
    )
    return len(calls)


async def _expire_events(
    session: AsyncSession,
    _storage: ObjectStorageAdapter,
    calls: list[CallRow],
    now: datetime,
) -> int:
    from turncall.services.call_view import ended_reasons

    # Freeze how the call ended, which the read derives from the events.
    frozen = await ended_reasons(session, calls)
    await call_repo.delete_call_events(
        session, [c.id for c in calls], except_types=TRANSCRIPT_EVENTS
    )
    for call in calls:
        call.ended_reason = frozen[call.id]
        call.events_expired_at = now
    return len(calls)


_EXPIRE = {
    RECORDING: _expire_recordings,
    TRANSCRIPT: _expire_transcripts,
    EVENTS: _expire_events,
}


def system_clock() -> datetime:
    return datetime.now(UTC)


async def run_janitor(
    session_factory: async_sessionmaker[AsyncSession],
    storage: Callable[[], ObjectStorageAdapter],
    *,
    clock: Clock = system_clock,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval` seconds until cancelled (the API's lifespan).
    `storage` is built per sweep, so a misconfigured backend fails the sweep,
    logged, rather than the server's start."""
    while True:
        try:
            await asyncio.sleep(interval)
            swept = await sweep(session_factory, storage(), now=clock())
            if swept.total:
                logger.info(
                    "retention_swept",
                    recordings=swept.recording,
                    transcripts=swept.transcript,
                    events=swept.events,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("retention_sweep_error")
