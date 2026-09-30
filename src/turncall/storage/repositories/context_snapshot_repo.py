"""Context snapshot repository — one row per agent segment of a call (#177)."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import CallContextSnapshotRow


async def create_snapshot(
    session: AsyncSession,
    *,
    call_id: UUID,
    agent_id: UUID | None,
    segment: int,
    system_instruction: str,
    messages: list[Any],
) -> CallContextSnapshotRow:
    row = CallContextSnapshotRow(
        call_id=call_id,
        agent_id=agent_id,
        segment=segment,
        system_instruction=system_instruction,
        messages=messages,
    )
    session.add(row)
    await session.flush()
    return row


async def list_snapshots(
    session: AsyncSession, call_id: UUID
) -> list[CallContextSnapshotRow]:
    """A call's snapshots in segment order."""
    result = await session.execute(
        select(CallContextSnapshotRow)
        .where(CallContextSnapshotRow.call_id == call_id)
        .order_by(CallContextSnapshotRow.segment)
    )
    return list(result.scalars().all())
