"""Takeaway repository — reusable post-call structured outputs (ADR-0013)."""

from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import AgentRow, TakeawayRow


async def create_takeaway(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    schema: dict[str, Any],
    description: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
) -> TakeawayRow:
    row = TakeawayRow(
        project_id=project_id,
        name=name,
        schema=schema,
        description=description,
        prompt=prompt,
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_id(
    session: AsyncSession, takeaway_id: UUID, *, project_id: UUID | None = None
) -> TakeawayRow | None:
    query = select(TakeawayRow).where(TakeawayRow.id == takeaway_id)
    if project_id is not None:
        query = query.where(TakeawayRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_by_name(
    session: AsyncSession, project_id: UUID, name: str
) -> TakeawayRow | None:
    result = await session.execute(
        select(TakeawayRow).where(
            TakeawayRow.project_id == project_id, TakeawayRow.name == name
        )
    )
    return result.scalar_one_or_none()


async def list_for_project(
    session: AsyncSession, project_id: UUID
) -> list[TakeawayRow]:
    result = await session.execute(
        select(TakeawayRow)
        .where(TakeawayRow.project_id == project_id)
        .order_by(TakeawayRow.created_at)
    )
    return list(result.scalars().all())


async def list_by_ids(
    session: AsyncSession, project_id: UUID, ids: list[UUID]
) -> list[TakeawayRow]:
    if not ids:
        return []
    result = await session.execute(
        select(TakeawayRow).where(
            TakeawayRow.project_id == project_id, TakeawayRow.id.in_(ids)
        )
    )
    return list(result.scalars().all())


async def update_takeaway(
    session: AsyncSession,
    takeaway_id: UUID,
    *,
    values: dict[str, Any],
) -> TakeawayRow | None:
    result = await session.execute(
        update(TakeawayRow)
        .where(TakeawayRow.id == takeaway_id)
        .values(**values)
        .returning(TakeawayRow)
    )
    return result.scalar_one_or_none()


async def delete_takeaway(session: AsyncSession, takeaway_id: UUID) -> bool:
    row = await get_by_id(session, takeaway_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def count_agents_referencing(
    session: AsyncSession, project_id: UUID, takeaway_id: UUID
) -> int:
    """Agents whose analysis.takeaway_ids contains this id (jsonb containment)."""
    result = await session.execute(
        select(func.count(AgentRow.id)).where(
            AgentRow.project_id == project_id,
            AgentRow.state != "archived",
            cast(AgentRow.config_blob["analysis"]["takeaway_ids"], String).contains(
                str(takeaway_id)
            ),
        )
    )
    return result.scalar_one()
