"""SMS session repository - data access for chat sessions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from turncall.domain.session_state import PENDING, REDACTION, SESSION_TTL_HOURS
from turncall.storage.models import ProjectRow, SmsSessionRow


async def create_session(
    session: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID | None,
    phone_number_id: UUID | None = None,
    customer_number: str,
    turncall_number: str,
    channel: str = "sms",
    metadata_json: dict | None = None,
    session_id: UUID | None = None,
) -> SmsSessionRow:
    """Create a new SMS/chat session.

    `session_id` is the id call-init was already told, when it ran first.
    """
    now = datetime.now(UTC)
    row = SmsSessionRow(
        **({"id": session_id} if session_id else {}),
        project_id=project_id,
        agent_id=agent_id,
        phone_number_id=phone_number_id,
        customer_number=customer_number,
        turncall_number=turncall_number,
        status="active",
        channel=channel,
        message_count=0,
        last_activity_at=now,
        expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
        metadata_json=metadata_json or {},
    )
    session.add(row)
    await session.flush()
    return row


async def set_session_metadata(
    session: AsyncSession,
    session_id: UUID,
    metadata_json: dict,
) -> SmsSessionRow | None:
    """Replace a session's metadata (its template variables, for one)."""
    result = await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .values(metadata_json=metadata_json)
        .returning(SmsSessionRow)
    )
    return result.scalar_one_or_none()


async def get_session_by_id(
    session: AsyncSession,
    session_id: UUID,
    *,
    project_id: UUID | None = None,
) -> SmsSessionRow | None:
    """Get a session by ID."""
    query = select(SmsSessionRow).where(SmsSessionRow.id == session_id)
    if project_id is not None:
        query = query.where(SmsSessionRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_active_session(
    session: AsyncSession,
    customer_number: str,
    turncall_number: str,
) -> SmsSessionRow | None:
    """Find an active session by phone number pair."""
    result = await session.execute(
        select(SmsSessionRow)
        .where(SmsSessionRow.customer_number == customer_number)
        .where(SmsSessionRow.turncall_number == turncall_number)
        .where(SmsSessionRow.status == "active")
        .order_by(SmsSessionRow.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_session_activity(
    session: AsyncSession,
    session_id: UUID,
) -> SmsSessionRow | None:
    """Bump last_activity_at and extends expires_at."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .values(
            last_activity_at=now,
            expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
            message_count=SmsSessionRow.message_count + 1,
        )
        .returning(SmsSessionRow)
    )
    return result.scalar_one_or_none()


def _ended_metadata() -> ColumnElement[Any]:
    """A session's metadata as it expires: marked for its rows' rewrite when
    its project redacts then (#314, `services/text_redaction`). Read off the
    project row, in the expiring statement, so every way a session ends marks
    it and a crash between the two cannot lose the mark."""
    redacts = (
        select(ProjectRow.redaction_enabled)
        .where(ProjectRow.id == SmsSessionRow.project_id)
        .scalar_subquery()
    )
    return case(
        (
            redacts.is_(True),
            SmsSessionRow.metadata_json.op("||")(
                func.jsonb_build_object(REDACTION, PENDING)
            ),
        ),
        else_=SmsSessionRow.metadata_json,
    )


async def expire_session(
    session: AsyncSession,
    session_id: UUID,
) -> SmsSessionRow | None:
    """Mark a session as expired."""
    result = await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .where(SmsSessionRow.status == "active")
        .values(status="expired", metadata_json=_ended_metadata())
        .returning(SmsSessionRow)
    )
    return result.scalar_one_or_none()


async def list_sessions(
    session: AsyncSession,
    project_id: UUID,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SmsSessionRow]:
    """List sessions for a project."""
    query = select(SmsSessionRow).where(SmsSessionRow.project_id == project_id)
    if status is not None:
        query = query.where(SmsSessionRow.status == status)
    query = query.order_by(SmsSessionRow.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_sessions(
    session: AsyncSession,
    project_id: UUID,
    *,
    status: str | None = None,
) -> int:
    """Count sessions for a project."""
    query = select(func.count(SmsSessionRow.id)).where(
        SmsSessionRow.project_id == project_id
    )
    if status is not None:
        query = query.where(SmsSessionRow.status == status)
    result = await session.execute(query)
    return result.scalar_one()


async def expire_stale_sessions(session: AsyncSession) -> int:
    """Bulk expire sessions past their TTL. Returns count of expired sessions."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.status == "active")
        .where(SmsSessionRow.expires_at < now)
        .values(status="expired", metadata_json=_ended_metadata())
    )
    return result.rowcount


async def list_pending_redaction(
    session: AsyncSession, *, limit: int = 50
) -> list[SmsSessionRow]:
    """Expired sessions whose rows are still to be rewritten redacted, the
    longest-waiting first."""
    result = await session.execute(
        select(SmsSessionRow)
        .where(SmsSessionRow.status == "expired")
        .where(SmsSessionRow.metadata_json[REDACTION].astext == PENDING)
        .order_by(SmsSessionRow.expires_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def set_session_redacted(
    session: AsyncSession,
    session_id: UUID,
    *,
    customer_number: str,
    turncall_number: str,
    state: str,
) -> None:
    """Store a session's numbers as redacted, and its redaction `state`."""
    await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .values(
            customer_number=customer_number,
            turncall_number=turncall_number,
            metadata_json=SmsSessionRow.metadata_json.op("||")(
                func.jsonb_build_object(REDACTION, state)
            ),
        )
    )


async def clear_pending_redaction(session: AsyncSession, session_id: UUID) -> None:
    """Drop the mark: the project stopped redacting before the rewrite ran."""
    await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .values(metadata_json=SmsSessionRow.metadata_json.op("-")(REDACTION))
    )
