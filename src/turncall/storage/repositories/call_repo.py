"""Call repository - data access for calls and call events."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> CallRow:
    """Create a new call record."""
    row = CallRow(
        project_id=project_id,
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
    """Update call status and optional fields. Returns updated row."""
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

    result = await session.execute(
        update(CallRow).where(CallRow.id == call_id).values(**values).returning(CallRow)
    )
    return result.scalar_one_or_none()


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
) -> CallRow | None:
    """Set the recording lifecycle status on a call record."""
    result = await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id)
        .values(recording_status=recording_status)
        .returning(CallRow)
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


async def update_call_analysis(
    session: AsyncSession,
    call_id: UUID,
    analysis_json: dict,
) -> CallRow | None:
    """Store analysis results on a call record."""
    result = await session.execute(
        update(CallRow)
        .where(CallRow.id == call_id)
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
