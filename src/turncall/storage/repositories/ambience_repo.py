"""Ambience sound repository (ADR-0021)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import AgentRow, AmbienceSoundRow


async def create_sound(
    session: AsyncSession,
    *,
    sound_id: UUID,
    project_id: UUID,
    name: str,
    duration_ms: int,
    renditions: dict[str, str],
) -> AmbienceSoundRow:
    row = AmbienceSoundRow(
        id=sound_id,
        project_id=project_id,
        name=name,
        duration_ms=duration_ms,
        renditions=renditions,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_id(
    session: AsyncSession, sound_id: UUID, *, project_id: UUID
) -> AmbienceSoundRow | None:
    """Always project-scoped: an id is never enough to reach a sound."""
    result = await session.execute(
        select(AmbienceSoundRow).where(
            AmbienceSoundRow.id == sound_id,
            AmbienceSoundRow.project_id == project_id,
        )
    )
    return result.scalar_one_or_none()


async def list_for_project(
    session: AsyncSession, project_id: UUID
) -> list[AmbienceSoundRow]:
    result = await session.execute(
        select(AmbienceSoundRow)
        .where(AmbienceSoundRow.project_id == project_id)
        .order_by(AmbienceSoundRow.created_at)
    )
    return list(result.scalars().all())


async def delete_sound(session: AsyncSession, row: AmbienceSoundRow) -> None:
    await session.delete(row)
    await session.flush()


async def agents_referencing(
    session: AsyncSession, project_id: UUID, sound_id: UUID
) -> list[AgentRow]:
    """Non-archived agents whose config names this sound — the takeaway rule."""
    result = await session.execute(
        select(AgentRow)
        .where(
            AgentRow.project_id == project_id,
            AgentRow.state != "archived",
            AgentRow.config_blob["ambience"]["sound"].astext == str(sound_id),
        )
        .order_by(AgentRow.name, AgentRow.version)
    )
    return list(result.scalars().all())


async def rendition_keys_for_project(
    session: AsyncSession, project_id: UUID
) -> list[str]:
    result = await session.execute(
        select(AmbienceSoundRow.renditions).where(
            AmbienceSoundRow.project_id == project_id
        )
    )
    rows: list[dict[str, str]] = list(result.scalars())
    return [key for renditions in rows for key in renditions.values()]
