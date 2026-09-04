"""SMS message repository - data access for chat messages."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import SmsMessageRow


async def create_message(
    session: AsyncSession,
    *,
    session_id: UUID,
    project_id: UUID,
    role: str,
    content: str,
    channel: str = "sms",
    provider_message_sid: str | None = None,
    token_count: int | None = None,
    metadata_json: dict | None = None,
) -> SmsMessageRow:
    """Create a new chat message."""
    row = SmsMessageRow(
        session_id=session_id,
        project_id=project_id,
        role=role,
        content=content,
        channel=channel,
        provider_message_sid=provider_message_sid,
        token_count=token_count,
        metadata_json=metadata_json or {},
    )
    session.add(row)
    await session.flush()
    return row


async def get_message_by_id(
    session: AsyncSession,
    message_id: UUID,
) -> SmsMessageRow | None:
    """Get a message by ID."""
    result = await session.execute(
        select(SmsMessageRow).where(SmsMessageRow.id == message_id)
    )
    return result.scalar_one_or_none()


async def list_messages_for_session(
    session: AsyncSession,
    session_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[SmsMessageRow]:
    """List messages for a session, ordered by creation time ascending."""
    result = await session.execute(
        select(SmsMessageRow)
        .where(SmsMessageRow.session_id == session_id)
        .order_by(SmsMessageRow.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_messages_for_session(
    session: AsyncSession,
    session_id: UUID,
) -> int:
    """Count messages in a session."""
    result = await session.execute(
        select(func.count(SmsMessageRow.id)).where(
            SmsMessageRow.session_id == session_id
        )
    )
    return result.scalar_one()
