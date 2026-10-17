"""Retention (#316, ADR-0024): how long a call's data is kept, and the janitor
that deletes it after.

A project sets it per kind (`PUT /v1/projects/{id}/retention`), in days from
the call's end; an agent's `retention` overrides any kind for its calls. Null
everywhere keeps everything, as before retention existed. Three kinds:

- **recording**: the audio in object storage (`recording_keys`, as the
  project purge sweeps them), a copy on the carrier's account (a Twilio
  recording, by the SID on its `recording.ready`), and `calls.recording_url`.
  `recording_status` becomes `expired`, and `recording.ready` loses its URLs
  (every `*_url` field): it names the recording, so it stays on the event log
  as long as the recording does, and goes with it if the rest of the events
  went first.
- **transcript**: what was said and what was made from it. The
  `TRANSCRIPT_EVENTS` on the call's event log (the lines, the tool calls with
  their arguments and results, messages injected into the conversation), the
  call's `tool_invocations` rows, and the call's `analysis_json` (summary,
  structured data, takeaways). The other events keep their place in the log,
  but every field of theirs that holds text (`redaction.text_fields`: a
  handoff's reason and context, a transfer's reason and message, DTMF digits,
  an error's text) is cleared with the transcript.
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
version: its calls take the project's. Changing what applies to an agent's
calls needs an admin key (`applied_by_name`, held by api/v1/agents).

The janitor touches `calls`, `call_events`, `tool_invocations`, recording
objects and carrier recordings, nothing else; the audit log (#317) keeps its
own retention. `tests/integration/test_retention_janitor.py` holds that
against the statements it runs.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import case, func, literal, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.adapters.storage import ObjectStorageAdapter
from turncall.domain.enums import AgentState, CallEventType, RecordingStatus
from turncall.domain.models import RetentionConfig
from turncall.services import redaction
from turncall.services.recording_storage import recording_keys
from turncall.storage.models import AgentRow, CallRow, ProjectRow
from turncall.storage.repositories import call_repo, tool_invocation_repo

if TYPE_CHECKING:
    from turncall.services.twilio_accounts import TwilioAccount

Clock = Callable[[], datetime]

# The kinds, as the janitor names them (`RetentionConfig.days`).
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
# Call events that name the recording, and are kept while it is.
RECORDING_EVENTS = frozenset({CallEventType.RECORDING_READY.value})

SWEEP_INTERVAL_SECONDS = 3600  # retention is in days; an hour late is in it
BATCH = 200  # calls per batch

# A carrier recording's deleter: the account it is on, and its SID.
CarrierDelete = Callable[["TwilioAccount", str], Awaitable[None]]


def of_project(row: ProjectRow | None) -> RetentionConfig:
    if row is None:
        return RetentionConfig()
    return RetentionConfig(
        recordings_days=row.retention_recordings_days,
        transcripts_days=row.retention_transcripts_days,
        events_days=row.retention_events_days,
    )


def of_agent_config(config_blob: dict[str, Any] | None) -> RetentionConfig:
    """An agent's override, from its stored config. A block a later build
    would refuse is no override (logged), never a failed sweep."""
    try:
        return RetentionConfig.model_validate(
            (config_blob or {}).get("retention") or {}
        )
    except ValidationError:
        logger.warning("agent_retention_unreadable")
        return RetentionConfig()


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


def _overrides(agents: list[Any]) -> dict[UUID, RetentionConfig]:
    """Every version's id → its name's live override, where it has one."""
    live = _live_versions(agents)
    out = {}
    for agent in agents:
        override = of_agent_config(live[(agent.project_id, agent.name)].config_blob)
        if not override.empty:
            out[agent.id] = override
    return out


async def for_call(session: AsyncSession, call: CallRow) -> RetentionConfig:
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


async def applied_by_name(
    session: AsyncSession, project_id: UUID, names: Iterable[str]
) -> dict[str, RetentionConfig]:
    """The override each named agent's calls get now: its live version's,
    empty for a name with no version. Read before and after a change to an
    agent, so a change to it can be told apart from a carried-over one."""
    wanted = set(names)
    rows = (
        (
            await session.execute(
                select(AgentRow)
                .where(AgentRow.project_id == project_id, AgentRow.name.in_(wanted))
                # After an update in this session: its rows as written.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    live = _live_versions(rows)
    return {
        name: of_agent_config(live[(project_id, name)].config_blob)
        if (project_id, name) in live
        else RetentionConfig()
        for name in wanted
    }


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
    base: RetentionConfig
    overrides: dict[UUID, RetentionConfig] = field(default_factory=dict)

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
    by_project: dict[UUID, dict[UUID, RetentionConfig]] = defaultdict(dict)
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


async def delete_at_twilio(account: TwilioAccount, recording_sid: str) -> None:
    """The carrier's copy of a recording, on the account it was made on."""
    await account.adapter().delete_recording(recording_sid)


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    storage: ObjectStorageAdapter,
    *,
    now: datetime,
    carrier: CarrierDelete = delete_at_twilio,
) -> Swept:
    """Delete every call's data that is past its retention at `now`. Safe on
    every replica at once: a batch of transcripts or events is locked, others
    skip it; a batch of recordings is marked only where no other sweep has."""
    async with session_factory() as session:
        plans = await _plans(session)
    swept = Swept()
    for plan in plans:
        for kind in KINDS:
            for days, scope in plan.cutoffs(kind):
                count = await _sweep_cutoff(
                    session_factory,
                    storage,
                    carrier,
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
    carrier: CarrierDelete,
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
        query = query.order_by(CallRow.created_at, CallRow.id).limit(BATCH)
        if kind == RECORDING:
            calls, done = await _expire_recordings(
                session_factory, storage, carrier, query, now
            )
        else:
            async with session_factory() as session:
                calls = list(
                    (await session.execute(query.with_for_update(skip_locked=True)))
                    .scalars()
                    .all()
                )
                done = await _EXPIRE[kind](session, calls, now) if calls else 0
                await session.commit()
        count += done
        if len(calls) < BATCH:
            return count
        after = (calls[-1].created_at, calls[-1].id)


# --- Recordings -----------------------------------------------------------


@dataclass(frozen=True)
class _Recording:
    """One call's recording, read before anything is deleted, so no row is
    locked and no transaction open while storage and the carrier answer."""

    call_id: UUID
    url: str | None
    keys: tuple[str, ...]
    carrier_sids: tuple[str, ...] = ()
    account: TwilioAccount | None = None


async def _expire_recordings(
    session_factory: async_sessionmaker[AsyncSession],
    storage: ObjectStorageAdapter,
    carrier: CarrierDelete,
    query: Any,
    now: datetime,
) -> tuple[list[CallRow], int]:
    """Collect a batch's recordings, delete them, then mark the calls; the
    calls read and how many were marked. Deletes first: a mark that fails
    leaves the call to the next sweep, which deletes again (idempotent),
    where the other order could orphan an object for good. A call whose
    recording could not be deleted is left unmarked and retried. Two
    replicas may delete the same objects; only one marks the call."""
    async with session_factory() as session:
        calls = list((await session.execute(query)).scalars().all())
        if not calls:
            return calls, 0
        sids = await _carrier_sids(session, [c.id for c in calls])
        collected = [await _collect(session, c, sids.get(c.id, ())) for c in calls]
    recordings = [r for r in collected if r is not None]

    failed = await storage.delete_many(
        sorted({key for r in recordings for key in r.keys})
    )
    deleted = []
    for recording in recordings:
        if errors := [failed[k] for k in recording.keys if k in failed]:
            logger.warning(
                "retention_recording_delete_failed call={call_id}: {err}",
                call_id=str(recording.call_id),
                err=errors[0],
            )
            continue
        if await _delete_at_carrier(carrier, recording):
            deleted.append(recording)
    if not deleted:
        return calls, 0

    async with session_factory() as session:
        marked = list(
            (
                await session.execute(
                    update(CallRow)
                    .where(
                        CallRow.recording_expired_at.is_(None),
                        # Unchanged since it was read: a URL written since
                        # names an object this sweep did not delete.
                        tuple_(
                            CallRow.id, func.coalesce(CallRow.recording_url, "")
                        ).in_([(r.call_id, r.url or "") for r in deleted]),
                    )
                    .values(
                        recording_url=None,
                        # `expired` where there was a recording to delete;
                        # `none` or `failed` stay as true as they were.
                        recording_status=case(
                            (
                                or_(
                                    CallRow.recording_url.is_not(None),
                                    CallRow.recording_status
                                    == RecordingStatus.COMPLETED.value,
                                ),
                                literal(
                                    RecordingStatus.EXPIRED.value,
                                    CallRow.recording_status.type,
                                ),
                            ),
                            else_=CallRow.recording_status,
                        ),
                        recording_expired_at=now,
                    )
                    .returning(CallRow.id, CallRow.events_expired_at)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        )
        await _forget_recording_events(
            session, {call_id: gone is not None for call_id, gone in marked}
        )
        await session.commit()
    return calls, len(marked)


async def _carrier_sids(
    session: AsyncSession, call_ids: list[UUID]
) -> dict[UUID, tuple[str, ...]]:
    """The carrier recordings each call's `recording.ready` events name: a
    Twilio recording's SID, where the account recorded the call itself."""
    out: dict[UUID, tuple[str, ...]] = defaultdict(tuple)
    events = await call_repo.list_call_event_payloads(
        session, call_ids, types=RECORDING_EVENTS
    )
    for _id, call_id, _type, payload in events:
        if sid := payload.get("recording_sid"):
            out[call_id] = (*out[call_id], sid)
    return out


async def _collect(
    session: AsyncSession, call: CallRow, carrier_sids: tuple[str, ...]
) -> _Recording | None:
    """What deleting `call`'s recording takes; None to leave it to the next
    sweep (its carrier account could not be read)."""
    recorded = (
        call.recording_url is not None
        or call.recording_status != RecordingStatus.NONE.value
    )
    keys = tuple(sorted(recording_keys(call.id, call.recording_url)))
    recording = _Recording(call.id, call.recording_url, keys if recorded else ())
    if not carrier_sids:
        return recording
    from turncall.config.settings import get_settings
    from turncall.services.twilio_accounts import account_for_call

    try:
        account = await account_for_call(session, call, get_settings())
    except Exception as exc:
        logger.warning(
            "retention_carrier_account_unreadable call={call_id}: {err}",
            call_id=str(call.id),
            err=exc,
        )
        return None
    return _Recording(
        call.id, call.recording_url, recording.keys, carrier_sids, account
    )


async def _delete_at_carrier(carrier: CarrierDelete, recording: _Recording) -> bool:
    """The carrier's copies of the recording deleted; False to retry next
    sweep. With no account to reach (no Twilio credentials at all, the number
    or the platform's), the copy is left and logged: nothing here can reach
    it, and retrying would keep everything else past its retention too."""
    for sid in recording.carrier_sids:
        if recording.account is None:
            logger.warning(
                "retention_carrier_recording_unreachable call={call_id} sid={sid}",
                call_id=str(recording.call_id),
                sid=sid,
            )
            continue
        try:
            await carrier(recording.account, sid)
        except Exception as exc:
            logger.warning(
                "retention_carrier_recording_delete_failed call={call_id} "
                "sid={sid}: {err}",
                call_id=str(recording.call_id),
                sid=sid,
                err=exc,
            )
            return False
    return True


async def _forget_recording_events(
    session: AsyncSession, events_gone: dict[UUID, bool]
) -> None:
    """`recording.ready` without the recording: its URLs (every `*_url`
    field: ours, and the carrier's) cleared, or the event deleted where the
    rest of the call's events already went (`events_gone`)."""
    if not events_gone:
        return
    events = await call_repo.list_call_event_payloads(
        session, list(events_gone), types=RECORDING_EVENTS
    )
    gone = []
    for event_id, call_id, _type, payload in events:
        if events_gone[call_id]:
            gone.append(event_id)
        else:
            urls = [k for k in payload if k.endswith("_url")]
            await call_repo.clear_event_fields(session, event_id, payload, urls)
    await call_repo.delete_call_events_by_id(session, gone)


# --- Transcripts and events -----------------------------------------------


async def _expire_transcripts(
    session: AsyncSession, calls: list[CallRow], now: datetime
) -> int:
    ids = [c.id for c in calls]
    await call_repo.delete_call_events(session, ids, types=TRANSCRIPT_EVENTS)
    await _clear_quoted_text(session, ids)
    await tool_invocation_repo.delete_for_calls(session, ids)
    await session.execute(
        update(CallRow)
        .where(CallRow.id.in_(ids))
        .values(analysis_json=None, transcript_expired_at=now)
        .execution_options(synchronize_session=False)
    )
    return len(calls)


async def _clear_quoted_text(session: AsyncSession, call_ids: list[UUID]) -> None:
    """The text the rest of the event log carries, set to null: the fields
    redaction analyses (`redaction.text_fields`), which quote the
    conversation or were made from it. The events themselves stay, with
    the events kind: `ended_reason` is derived from which ones exist."""
    for (
        event_id,
        _call,
        event_type,
        payload,
    ) in await call_repo.list_call_event_payloads(session, call_ids):
        if text := redaction.text_fields(payload, event_type=event_type):
            await call_repo.clear_event_fields(session, event_id, payload, text)


async def _expire_events(
    session: AsyncSession, calls: list[CallRow], now: datetime
) -> int:
    from turncall.services.call_view import ended_reasons

    # Freeze how the call ended, which the read derives from the events.
    frozen = await ended_reasons(session, calls)
    # `recording.ready` stays while the recording does; it goes with it.
    for recorded, kept in (
        (True, TRANSCRIPT_EVENTS | RECORDING_EVENTS),
        (False, TRANSCRIPT_EVENTS),
    ):
        ids = [c.id for c in calls if (c.recording_expired_at is None) == recorded]
        if ids:
            await call_repo.delete_call_events(session, ids, except_types=kept)
    for call in calls:
        call.ended_reason = frozen[call.id]
        call.events_expired_at = now
    return len(calls)


_EXPIRE = {
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
