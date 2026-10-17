"""SMS message repository - data access for chat messages."""

from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.session_state import DONE, PENDING, REDACTION
from turncall.storage.models import SmsMessageRow, SmsSessionRow


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
    """Create a new chat message.

    Its session's row is updated with it, in the same transaction: a session
    already rewritten redacted (a reply that finished after its session
    ended) goes back to pending, so the sweep rewrites this message too. The
    update is unconditional so that it takes the row's lock, which the
    rewrite holds until it commits (`sms_session_repo.lock_for_redaction`):
    one of the two always sees the other (#314).
    """
    await session.execute(
        update(SmsSessionRow)
        .where(SmsSessionRow.id == session_id)
        .values(
            metadata_json=case(
                (
                    SmsSessionRow.metadata_json[REDACTION].astext == DONE,
                    SmsSessionRow.metadata_json.op("||")(
                        func.jsonb_build_object(REDACTION, PENDING)
                    ),
                ),
                else_=SmsSessionRow.metadata_json,
            )
        )
        .execution_options(synchronize_session=False)
    )
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


async def list_unredacted_messages(
    session: AsyncSession, session_id: UUID
) -> list[tuple[UUID, str]]:
    """(id, content) of a session's messages not yet rewritten redacted."""
    result = await session.execute(
        select(SmsMessageRow.id, SmsMessageRow.content)
        .where(SmsMessageRow.session_id == session_id)
        .where(SmsMessageRow.metadata_json[REDACTION].astext.is_distinct_from(DONE))
        .order_by(SmsMessageRow.created_at.asc())
    )
    return [(row.id, row.content) for row in result]


async def set_message_redacted(
    session: AsyncSession, message_id: UUID, content: str
) -> None:
    """Store a message's content as redacted, and mark it so."""
    await session.execute(
        update(SmsMessageRow)
        .where(SmsMessageRow.id == message_id)
        .values(
            content=content,
            metadata_json=SmsMessageRow.metadata_json.op("||")(
                func.jsonb_build_object(REDACTION, DONE)
            ),
        )
    )
