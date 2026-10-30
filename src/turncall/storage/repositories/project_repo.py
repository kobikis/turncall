"""Project repository - data access for projects.

Soft delete (ADR-0011): a project with deleted_at set is gone. Every lookup here
filters it out, so nothing (auth, GET, list) sees a soft-deleted project.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


async def set_limits(
    session: AsyncSession, project_id: UUID, **limits: int | None
) -> ProjectRow | None:
    """Replace a live project's limits (#206); `limits` are `limit_*` columns."""
    row = await get_project_by_id(session, project_id)
    if row is None:
        return None
    for column, value in limits.items():
        setattr(row, column, value)
    await session.flush()
    await session.refresh(row)
    return row


async def set_redaction(
    session: AsyncSession,
    project_id: UUID,
    *,
    enabled: bool,
    entities: list[str] | None,
) -> ProjectRow | None:
    """Replace a live project's redaction (#313); `entities` None = the
    platform's default list."""
    row = await get_project_by_id(session, project_id)
    if row is None:
        return None
    row.redaction_enabled = enabled
    row.redaction_entities = entities
    await session.flush()
    await session.refresh(row)
    return row


async def set_retention(
    session: AsyncSession,
    project_id: UUID,
    *,
    recordings_days: int | None,
    transcripts_days: int | None,
    events_days: int | None,
) -> ProjectRow | None:
    """Replace a live project's retention (#316); None = kept for good."""
    row = await get_project_by_id(session, project_id)
    if row is None:
        return None
    row.retention_recordings_days = recordings_days
    row.retention_transcripts_days = transcripts_days
    row.retention_events_days = events_days
    await session.flush()
    await session.refresh(row)
    return row


async def lock_skill_library_links(session: AsyncSession) -> None:
    """Serialize every change to a skill library link for the transaction:
    two links checked at once (A to B, B to C) would each see no chain and
    together make one."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("skill-library-links")))
    )


async def set_skill_library(
    session: AsyncSession, project_id: UUID, library_id: UUID | None
) -> ProjectRow | None:
    """Link a live project to a skill library (#432), or unlink it (None)."""
    row = await get_project_by_id(session, project_id)
    if row is None:
        return None
    row.skill_library_id = library_id
    await session.flush()
    await session.refresh(row)
    return row


async def skill_library_of(
    session: AsyncSession, project_id: UUID
) -> tuple[UUID, bool] | None:
    """The project's skill library and whether it is live (not soft-deleted),
    or None for a project linked to none."""
    library = aliased(ProjectRow)
    result = await session.execute(
        select(ProjectRow.skill_library_id, library.deleted_at)
        .outerjoin(library, library.id == ProjectRow.skill_library_id)
        .where(ProjectRow.id == project_id)
    )
    found = result.one_or_none()
    if found is None or found[0] is None:
        return None
    return found[0], found[1] is None


async def projects_linked_to(session: AsyncSession, library_id: UUID) -> list[UUID]:
    """The live projects whose skill library is `library_id`."""
    result = await session.execute(
        select(ProjectRow.id)
        .where(
            ProjectRow.skill_library_id == library_id,
            ProjectRow.deleted_at.is_(None),
        )
        .order_by(ProjectRow.created_at, ProjectRow.id)
    )
    return list(result.scalars().all())


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
