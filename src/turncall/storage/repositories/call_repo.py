"""Call repository - data access for calls and call events."""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.carriers import DEFAULT_CARRIER
from turncall.domain.enums import CallEventType
from turncall.storage.models import CallEventRow, CallRow


async def create_call(
    session: AsyncSession,
    *,
    project_id: UUID,
    direction: str,
    from_number: str | None = None,
    to_number: str | None = None,
    active_agent_id: UUID | None = None,
    workflow_id: UUID | None = None,
    provider_call_sid: str | None = None,
    metadata_json: dict | None = None,
    call_id: UUID | None = None,
    carrier: str = DEFAULT_CARRIER,
) -> CallRow:
    """Create a new call record. `call_id` when the id was needed before the
    row existed — a live-call slot is taken under it (#224). `carrier` is
    stored as `calls.provider`."""
    row = CallRow(
        **({"id": call_id} if call_id is not None else {}),
        project_id=project_id,
        provider=carrier,
        direction=direction,
        from_number=from_number,
        to_number=to_number,
        active_agent_id=active_agent_id,
        workflow_id=workflow_id,
        provider_call_sid=provider_call_sid,
        status="initiated",
        metadata_json=metadata_json or {},
    )
    session.add(row)
    await session.flush()
    return row


async def get_call_by_id(
    session: AsyncSession,
    call_id: UUID,
    *,
    project_id: UUID | None = None,
) -> CallRow | None:
    """Get a call by ID."""
    query = select(CallRow).where(CallRow.id == call_id)
    if project_id is not None:
        query = query.where(CallRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_call_by_provider_sid(
    session: AsyncSession,
    provider_call_sid: str,
) -> CallRow | None:
    """Get a call by Twilio Call SID."""
    result = await session.execute(
        select(CallRow).where(CallRow.provider_call_sid == provider_call_sid)
    )
    return result.scalar_one_or_none()


async def update_call_status(
    session: AsyncSession,
    call_id: UUID,
    *,
    status: str,
    provider_call_sid: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    duration_ms: int | None = None,
    active_agent_id: UUID | None = None,
) -> CallRow | None:
    """Update call status and optional fields. Returns updated row, or None
    when there is none to update.

    An ended call is never reopened: a status that isn't an ending lands only
    on a call that hasn't ended. A pipeline starting, or a transfer failing
    back to `in_progress`, after the carrier ended the call used to make it
    live again, and its ending then sent `call.ended` a second time (#328).
    """
    values: dict[str, Any] = {"status": status}
    if provider_call_sid is not None:
        values["provider_call_sid"] = provider_call_sid
    if started_at is not None:
        values["started_at"] = started_at
    if ended_at is not None:
        values["ended_at"] = ended_at
    if duration_ms is not None:
        values["duration_ms"] = duration_ms
    if active_agent_id is not None:
        values["active_agent_id"] = active_agent_id

    stmt = update(CallRow).where(CallRow.id == call_id)
    if status not in _ENDED:
        stmt = stmt.where(CallRow.status.not_in(_ENDED))
    result = await session.execute(stmt.values(**values).returning(CallRow))
    return result.scalar_one_or_none()


# A call in one of these is over: no stream may start one again.
_ENDED = ("completed", "failed", "no_answer", "busy")


async def attach_media(session: AsyncSession, call_id: UUID, *, carrier: str) -> bool:
    """Claim the call's one media stream; whether this caller got it.

    A guarded UPDATE, so of two streams racing for one call exactly one wins,
    whichever replica each landed on. Lands only on a call on `carrier`, not
    yet attached and not over. The caller commits.
    """
    result = await session.execute(
        update(CallRow)
        .where(
            CallRow.id == call_id,
            CallRow.provider == carrier,
            CallRow.media_attached_at.is_(None),
            CallRow.status.not_in(_ENDED),
        )
        .values(media_attached_at=func.now())
    )
    return bool(result.rowcount)


async def mark_call_ended(
    session: AsyncSession,
    call_id: UUID,
    *,
    status: str,
    ended_at: datetime,
    duration_ms: int | None = None,
) -> CallRow | None:
    """End the call: its terminal status, `ended_at` and duration, unless it is
    already over. The row if this caller ended it, else None.

    A guarded UPDATE, as `attach_media`: of the paths that end a call — its
    pipeline finishing, the carrier's status callback, the end API — exactly
    one does (#328). Which of them starts post-call processing, which sends
    `call.ended`, is `claim_post_call`'s to settle (#356). The caller commits,
    and must before starting post-call processing, which reads the call in a
    session of its own.
    """
    values: dict[str, Any] = {"status": status, "ended_at": ended_at}
    if duration_ms is not None:
        values["duration_ms"] = duration_ms
    result = await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id, CallRow.status.not_in(_ENDED))
        .values(**values)
        .returning(CallRow)
    )
    return result.scalar_one_or_none()


async def defer_post_call(session: AsyncSession, call_id: UUID) -> bool:
    """Leave the call's post-call processing to its pipeline, which starts it
    once its last lines are stored (#356); whether this deferred it. The
    carrier's status callback and the end API, on a call a pipeline runs.
    The sweep starts it for a pipeline that never does. The caller commits.
    """
    result = await session.execute(
        update(CallRow)
        .where(
            CallRow.id == call_id,
            CallRow.post_call_deferred_at.is_(None),
            CallRow.post_call_started_at.is_(None),
        )
        .values(post_call_deferred_at=func.now())
    )
    return bool(result.rowcount)


async def claim_post_call(
    session: AsyncSession, call_id: UUID, *, deferred: bool = False
) -> bool:
    """Claim the call's one post-call processing run; whether this caller got
    it. `deferred`: only if an ending left it to the call's pipeline
    (`defer_post_call`) — the pipeline on a call something else ended, and
    the sweep.

    A guarded UPDATE, as `mark_call_ended`: of the paths that may start it —
    the pipeline finishing, the carrier's status callback or the end API on a
    call no pipeline ran, the sweep — exactly one does, so `call.ended` is
    sent once (#356). The caller commits, before starting it.
    """
    stmt = update(CallRow).where(
        CallRow.id == call_id, CallRow.post_call_started_at.is_(None)
    )
    if deferred:
        stmt = stmt.where(CallRow.post_call_deferred_at.is_not(None))
    result = await session.execute(stmt.values(post_call_started_at=func.now()))
    return bool(result.rowcount)


async def list_post_call_deferred(
    session: AsyncSession, *, deferred_before: datetime, limit: int = 100
) -> list[CallRow]:
    """Calls deferred to their pipeline before `deferred_before` whose
    post-call processing has not started: the pipeline has not finished, or
    never will (#356)."""
    result = await session.execute(
        select(CallRow)
        .where(
            CallRow.post_call_deferred_at.is_not(None),
            CallRow.post_call_deferred_at < deferred_before,
            CallRow.post_call_started_at.is_(None),
        )
        .order_by(CallRow.post_call_deferred_at)
        .limit(limit)
    )
    return list(result.scalars())


async def update_call_recording_url(
    session: AsyncSession,
    call_id: UUID,
    recording_url: str,
) -> CallRow | None:
    """Store the recording URL on a call record."""
    result = await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id)
        .values(recording_url=recording_url)
        .returning(CallRow)
    )
    return result.scalar_one_or_none()


async def update_call_recording_status(
    session: AsyncSession,
    call_id: UUID,
    recording_status: str,
    *,
    only_from: str | None = None,
) -> CallRow | None:
    """Set the recording lifecycle status on a call record.

    `only_from`: change it only while it is still that value — so a write that
    lands late cannot take back a status already moved past it.
    """
    stmt = update(CallRow).where(CallRow.id == call_id)
    if only_from is not None:
        stmt = stmt.where(CallRow.recording_status == only_from)
    result = await session.execute(
        stmt.values(recording_status=recording_status).returning(CallRow)
    )
    return result.scalar_one_or_none()


async def list_calls(
    session: AsyncSession,
    project_id: UUID,
    *,
    status: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CallRow]:
    """List calls for a project."""
    query = select(CallRow).where(CallRow.project_id == project_id)
    if status is not None:
        query = query.where(CallRow.status == status)
    if direction is not None:
        query = query.where(CallRow.direction == direction)
    query = query.order_by(CallRow.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_calls(
    session: AsyncSession,
    project_id: UUID,
    *,
    status: str | None = None,
) -> int:
    """Count calls for a project."""
    query = select(func.count(CallRow.id)).where(CallRow.project_id == project_id)
    if status is not None:
        query = query.where(CallRow.status == status)
    result = await session.execute(query)
    return result.scalar_one()


# --- Call Events ---


async def create_call_event(
    session: AsyncSession,
    *,
    call_id: UUID,
    event_type: str,
    payload: dict | None = None,
    provider_timestamp: datetime | None = None,
    sequence_number: int = 0,
) -> CallEventRow:
    """Create a new call event."""
    row = CallEventRow(
        call_id=call_id,
        event_type=event_type,
        payload=payload or {},
        provider_timestamp=provider_timestamp,
        internal_timestamp=datetime.now(UTC),
        sequence_number=sequence_number,
    )
    session.add(row)
    await session.flush()
    return row


async def list_event_types(session: AsyncSession, call_id: UUID) -> set[str]:
    """Return the distinct event_type strings recorded for a call.

    One query, no row limit — used to derive ended_reason without the
    truncation risk of list_call_events' limit (terminal signals fire late).
    """
    result = await session.execute(
        select(CallEventRow.event_type)
        .where(CallEventRow.call_id == call_id)
        .distinct()
    )
    return set(result.scalars().all())


async def list_event_types_by_call(
    session: AsyncSession, call_ids: list[UUID]
) -> dict[UUID, set[str]]:
    """`list_event_types` for a page of calls, in one query."""
    result = await session.execute(
        select(CallEventRow.call_id, CallEventRow.event_type)
        .where(CallEventRow.call_id.in_(call_ids))
        .distinct()
    )
    by_call: dict[UUID, set[str]] = {}
    for call_id, event_type in result.all():
        by_call.setdefault(call_id, set()).add(event_type)
    return by_call


async def list_control_ended_calls(
    session: AsyncSession, call_ids: list[UUID]
) -> set[UUID]:
    """The calls among `call_ids` the end_call tool hung up (ADR-0008)."""
    result = await session.execute(
        select(CallEventRow.call_id)
        .where(
            CallEventRow.call_id.in_(call_ids),
            CallEventRow.event_type == CallEventType.CALL_ENDED,
            CallEventRow.payload["source"].astext == "control",
        )
        .distinct()
    )
    return set(result.scalars().all())


async def list_payloads_by_call(
    session: AsyncSession, call_ids: list[UUID], event_type: CallEventType
) -> dict[UUID, list[dict[str, Any]]]:
    """One event type's payloads for a page of calls, in order, one query."""
    result = await session.execute(
        select(CallEventRow.call_id, CallEventRow.payload)
        .where(
            CallEventRow.call_id.in_(call_ids),
            CallEventRow.event_type == event_type,
        )
        .order_by(CallEventRow.call_id, CallEventRow.sequence_number)
    )
    by_call: dict[UUID, list[dict[str, Any]]] = {}
    for row in result.all():
        by_call.setdefault(row.call_id, []).append(row.payload)
    return by_call


async def list_call_events(
    session: AsyncSession,
    call_id: UUID,
    *,
    event_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[CallEventRow]:
    """List events for a call."""
    query = select(CallEventRow).where(CallEventRow.call_id == call_id)
    if event_type is not None:
        query = query.where(CallEventRow.event_type == event_type)
    query = (
        query.order_by(CallEventRow.sequence_number.asc()).limit(limit).offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def delete_call_events(
    session: AsyncSession,
    call_ids: list[UUID],
    *,
    types: Iterable[str] | None = None,
    except_types: Iterable[str] = (),
) -> None:
    """Delete calls' events, of `types` only or of every type but
    `except_types`. The retention janitor's (#316); nothing else deletes
    events but a project's purge cascade."""
    query = delete(CallEventRow).where(CallEventRow.call_id.in_(call_ids))
    if types is not None:
        query = query.where(CallEventRow.event_type.in_(list(types)))
    if except_types := list(except_types):
        query = query.where(CallEventRow.event_type.not_in(except_types))
    await session.execute(query)


async def list_call_event_payloads(
    session: AsyncSession,
    call_ids: list[UUID],
    *,
    types: Iterable[str] | None = None,
) -> list[tuple[UUID, UUID, str, dict[str, Any]]]:
    """`(event id, call id, type, payload)` of calls' events, of `types`
    only if given, in order. For the retention janitor (#316), which clears
    fields of them."""
    query = select(
        CallEventRow.id,
        CallEventRow.call_id,
        CallEventRow.event_type,
        CallEventRow.payload,
    ).where(CallEventRow.call_id.in_(call_ids))
    if types is not None:
        query = query.where(CallEventRow.event_type.in_(list(types)))
    result = await session.execute(
        query.order_by(CallEventRow.call_id, CallEventRow.sequence_number)
    )
    return [(r.id, r.call_id, r.event_type, r.payload) for r in result.all()]


async def clear_event_fields(
    session: AsyncSession, event_id: UUID, payload: dict[str, Any], keys: Iterable[str]
) -> None:
    """Set `keys` of one event's payload to null: the retention janitor's,
    when what they held expired (#316). Takes text out, never puts it in."""
    cleared = set(keys)
    await session.execute(
        update(CallEventRow)
        .where(CallEventRow.id == event_id)
        .values(payload={k: None if k in cleared else v for k, v in payload.items()})
    )


async def delete_call_events_by_id(
    session: AsyncSession, event_ids: list[UUID]
) -> None:
    """Delete events by id: the retention janitor's (#316)."""
    if event_ids:
        await session.execute(
            delete(CallEventRow).where(CallEventRow.id.in_(event_ids))
        )


async def update_call_analysis(
    session: AsyncSession,
    call_id: UUID,
    analysis_json: dict,
) -> CallRow | None:
    """Store analysis results on a call record. None, and nothing written,
    when the call's transcript has expired (#316): a run that read it before
    the janitor deleted it must not store what it made from it after."""
    result = await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id, CallRow.transcript_expired_at.is_(None))
        .values(analysis_json=analysis_json)
        .returning(CallRow)
    )
    return result.scalar_one_or_none()


async def get_next_sequence_number(session: AsyncSession, call_id: UUID) -> int:
    """Get the next sequence number for call events.

    Concurrent writers (transcript taps, lifecycle + handoff events) otherwise
    read the same MAX and collide on the (call_id, sequence_number) unique
    constraint. A per-call advisory lock serializes the read-then-insert; it is
    held until the caller's transaction commits, so the INSERT must happen in the
    same transaction as this call (every caller does that today). Auto-released
    on commit/rollback — no race, no retry loop.
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(str(call_id))))
    )
    result = await session.execute(
        select(func.coalesce(func.max(CallEventRow.sequence_number), 0)).where(
            CallEventRow.call_id == call_id
        )
    )
    return result.scalar_one() + 1
