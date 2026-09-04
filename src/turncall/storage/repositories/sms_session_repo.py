"""SMS session repository - data access for chat sessions."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.session_state import SESSION_TTL_HOURS
from turncall.storage.models import SmsSessionRow


async def create_session(
    session: AsyncSession,
    *,
    project_id: UUID,
    agent_id: UUID,
    phone_number_id: UUID | None = None,
    customer_number: str,
    turncall_number: str,
    channel: str = "sms",
    metadata_json: dict | None = None,
) -> SmsSessionRow:
    """Create a new SMS/chat session."""
    now = datetime.now(UTC)
    row = SmsSessionRow(
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


async def expire_session(
    session: AsyncSession,
    session_id: UUID,
) -> SmsSessionRow | None:
    """Mark a session as expired."""
    result = await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .where(SmsSessionRow.status == "active")
        .values(status="expired")
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
        .values(status="expired")
    )
    return result.rowcount
