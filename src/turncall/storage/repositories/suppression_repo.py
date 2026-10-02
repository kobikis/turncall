"""Suppression list repository — numbers a project never dials (#227)."""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import SuppressedNumberRow


async def add(
    session: AsyncSession, *, project_id: UUID, number: str
) -> SuppressedNumberRow:
    """Suppress `number`; adding one already listed returns the existing entry."""
    await session.execute(
        insert(SuppressedNumberRow)
        .values(project_id=project_id, e164_number=number)
        .on_conflict_do_nothing(constraint="uq_suppressed_numbers_project_number")
    )
    result = await session.execute(
        select(SuppressedNumberRow).where(
            SuppressedNumberRow.project_id == project_id,
            SuppressedNumberRow.e164_number == number,
        )
    )
    return result.scalar_one()


async def list_for_project(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int,
    offset: int,
    number: str | None = None,
) -> tuple[list[SuppressedNumberRow], int]:
    """A page of the list, newest first, and the total it pages over."""
    where = [SuppressedNumberRow.project_id == project_id]
    if number is not None:
        where.append(SuppressedNumberRow.e164_number == number)
    total = await session.scalar(
        select(func.count(SuppressedNumberRow.id)).where(*where)
    )
    result = await session.execute(
        select(SuppressedNumberRow)
        .where(*where)
        .order_by(SuppressedNumberRow.created_at.desc(), SuppressedNumberRow.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total or 0


async def remove(session: AsyncSession, *, project_id: UUID, entry_id: UUID) -> bool:
    result = await session.execute(
        delete(SuppressedNumberRow).where(
            SuppressedNumberRow.id == entry_id,
            SuppressedNumberRow.project_id == project_id,
        )
        .returning(SuppressedNumberRow.id)
    )
    return result.scalar_one_or_none() is not None


async def is_suppressed(
    session: AsyncSession, *, project_id: UUID, number: str
) -> bool:
    return bool(
        await session.scalar(
            select(
                select(SuppressedNumberRow.id)
                .where(
                    SuppressedNumberRow.project_id == project_id,
                    SuppressedNumberRow.e164_number == number,
                )
                .exists()
            )
        )
    )
