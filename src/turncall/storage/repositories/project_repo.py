"""Project repository - data access for projects.

Soft delete (ADR-0011): a project with deleted_at set is gone. Every lookup here
filters it out, so nothing (auth, GET, list) sees a soft-deleted project.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import ProjectRow, utc_now


async def create_project(session: AsyncSession, *, name: str) -> ProjectRow:
    """Create a new project."""
    row = ProjectRow(name=name)
    session.add(row)
    await session.flush()
    return row


async def get_project_by_id(
    session: AsyncSession, project_id: UUID
) -> ProjectRow | None:
    """Get a live (non-soft-deleted) project by ID."""
    result = await session.execute(
        select(ProjectRow).where(
            ProjectRow.id == project_id,
            ProjectRow.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_projects(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[ProjectRow]:
    """List all live projects with pagination."""
    result = await session.execute(
        select(ProjectRow)
        .where(ProjectRow.deleted_at.is_(None))
        .order_by(ProjectRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_projects(session: AsyncSession) -> int:
    """Count live projects."""
    from sqlalchemy import func

    result = await session.execute(
        select(func.count(ProjectRow.id)).where(ProjectRow.deleted_at.is_(None))
    )
    return result.scalar_one()


async def soft_delete_project(session: AsyncSession, project_id: UUID) -> None:
    """Mark a project deleted. Rows stay; a purge job hard-deletes later."""
    row = await get_project_by_id(session, project_id)
    if row is not None:
        row.deleted_at = utc_now()
        await session.flush()


async def list_purgeable_project_ids(
    session: AsyncSession, cutoff: datetime
) -> list[UUID]:
    """IDs of projects soft-deleted before `cutoff` — ready for hard delete."""
    result = await session.execute(
        select(ProjectRow.id).where(
            ProjectRow.deleted_at.is_not(None),
            ProjectRow.deleted_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def hard_delete_project(session: AsyncSession, project_id: UUID) -> None:
    """Physically delete a project — the FK cascade reclaims its whole tree
    (agents, calls, KBs, keys, …). Purge job only; use soft_delete otherwise."""
    await session.execute(delete(ProjectRow).where(ProjectRow.id == project_id))
