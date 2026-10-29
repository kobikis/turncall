"""Skill repository (#423): each row is one version of a named skill.

The versioning mirrors `agent_repo`: a create is the next version of its name,
only a draft is updated, a publish freezes a draft, and a rollback restores an
archived version. Every state move is a guarded UPDATE from the state it
expects, so a concurrent move makes it return None rather than land twice.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import SkillState
from turncall.storage.models import SkillRow


async def lock_name(session: AsyncSession, project_id: UUID, name: str) -> None:
    """Serialize the writes to one skill name for the transaction: two creates
    would pick the same next version, and two publishes would each archive
    the other's and leave two live."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"skill:{project_id}:{name}")))
    )


async def next_version(session: AsyncSession, project_id: UUID, name: str) -> int:
    result = await session.execute(
        select(func.max(SkillRow.version)).where(
            SkillRow.project_id == project_id, SkillRow.name == name
        )
    )
    return (result.scalar_one() or 0) + 1


async def create_skill(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    version: int,
    description: str,
    instructions: str,
    tools: list[dict[str, Any]],
    knowledge_bases: list[dict[str, Any]],
) -> SkillRow:
    """A new draft version."""
    row = SkillRow(
        project_id=project_id,
        name=name,
        version=version,
        state=SkillState.DRAFT.value,
        description=description,
        instructions=instructions,
        tools=tools,
        knowledge_bases=knowledge_bases,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_id(
    session: AsyncSession, skill_id: UUID, *, project_id: UUID
) -> SkillRow | None:
    result = await session.execute(
        select(SkillRow).where(
            SkillRow.id == skill_id, SkillRow.project_id == project_id
        )
    )
    return result.scalar_one_or_none()


async def get_published(
    session: AsyncSession, project_id: UUID, name: str
) -> SkillRow | None:
    """The live version of a named skill, if one is published."""
    result = await session.execute(
        select(SkillRow)
        .where(
            SkillRow.project_id == project_id,
            SkillRow.name == name,
            SkillRow.state == SkillState.PUBLISHED.value,
        )
        .order_by(SkillRow.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _filtered(query: Any, name: str | None, state: str | None) -> Any:
    if name is not None:
        query = query.where(SkillRow.name == name)
    if state is not None:
        query = query.where(SkillRow.state == state)
    return query


async def list_skills(
    session: AsyncSession,
    project_id: UUID,
    *,
    name: str | None = None,
    state: str | None = None,
    limit: int,
    offset: int,
) -> list[SkillRow]:
    """Newest first; `version` then `id` break ties, so a page cannot repeat
    or skip a row."""
    query = _filtered(
        select(SkillRow).where(SkillRow.project_id == project_id), name, state
    )
    result = await session.execute(
        query.order_by(
            SkillRow.created_at.desc(), SkillRow.version.desc(), SkillRow.id.desc()
        )
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_skills(
    session: AsyncSession,
    project_id: UUID,
    *,
    name: str | None = None,
    state: str | None = None,
) -> int:
    query = _filtered(
        select(func.count(SkillRow.id)).where(SkillRow.project_id == project_id),
        name,
        state,
    )
    count: int = (await session.execute(query)).scalar_one()
    return count


async def update_draft(
    session: AsyncSession, skill_id: UUID, *, values: dict[str, Any]
) -> SkillRow | None:
    """Change a draft's content. None if it is no longer a draft."""
    result = await session.execute(
        update(SkillRow)
        .where(SkillRow.id == skill_id, SkillRow.state == SkillState.DRAFT.value)
        .values(**values)
        .returning(SkillRow)
    )
    return result.scalar_one_or_none()


async def publish(session: AsyncSession, skill_id: UUID) -> SkillRow | None:
    """Freeze a draft as published. None if it is no longer a draft."""
    result = await session.execute(
        update(SkillRow)
        .where(SkillRow.id == skill_id, SkillRow.state == SkillState.DRAFT.value)
        .values(state=SkillState.PUBLISHED.value, published_at=datetime.now(UTC))
        .returning(SkillRow)
    )
    return result.scalar_one_or_none()


async def restore(session: AsyncSession, skill_id: UUID) -> SkillRow | None:
    """An archived version back to published. None if it is not archived."""
    result = await session.execute(
        update(SkillRow)
        .where(SkillRow.id == skill_id, SkillRow.state == SkillState.ARCHIVED.value)
        .values(state=SkillState.PUBLISHED.value)
        .returning(SkillRow)
    )
    return result.scalar_one_or_none()


async def archive_other_published(
    session: AsyncSession, project_id: UUID, name: str, *, keep: UUID
) -> list[UUID]:
    """Archive every published version of `name` but `keep`; their ids."""
    result = await session.execute(
        update(SkillRow)
        .where(
            SkillRow.project_id == project_id,
            SkillRow.name == name,
            SkillRow.state == SkillState.PUBLISHED.value,
            SkillRow.id != keep,
        )
        .values(state=SkillState.ARCHIVED.value)
        .returning(SkillRow.id)
    )
    return sorted(result.scalars().all())
