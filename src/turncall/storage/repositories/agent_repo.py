"""Agent repository - data access for agents."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import AgentRow, PhoneNumberRow


async def create_agent(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    environment: str = "development",
    version: int = 1,
    config_blob: dict,
) -> AgentRow:
    """Create a new draft agent."""
    row = AgentRow(
        project_id=project_id,
        name=name,
        environment=environment,
        version=version,
        state="draft",
        config_blob=config_blob,
    )
    session.add(row)
    await session.flush()
    return row


async def get_agent_by_id(
    session: AsyncSession,
    agent_id: UUID,
    *,
    project_id: UUID | None = None,
) -> AgentRow | None:
    """Get an agent by ID, optionally scoped to project."""
    query = select(AgentRow).where(AgentRow.id == agent_id)
    if project_id is not None:
        query = query.where(AgentRow.project_id == project_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def list_agents(
    session: AsyncSession,
    project_id: UUID,
    *,
    environment: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRow]:
    """List agents for a project with optional filters."""
    query = select(AgentRow).where(AgentRow.project_id == project_id)
    if environment is not None:
        query = query.where(AgentRow.environment == environment)
    if state is not None:
        query = query.where(AgentRow.state == state)
    query = query.order_by(AgentRow.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def count_agents(
    session: AsyncSession,
    project_id: UUID,
    *,
    environment: str | None = None,
    state: str | None = None,
) -> int:
    """Count agents for a project."""
    query = select(func.count(AgentRow.id)).where(AgentRow.project_id == project_id)
    if environment is not None:
        query = query.where(AgentRow.environment == environment)
    if state is not None:
        query = query.where(AgentRow.state == state)
    result = await session.execute(query)
    return result.scalar_one()


async def update_agent_config(
    session: AsyncSession,
    agent_id: UUID,
    *,
    config_blob: dict,
    name: str | None = None,
) -> AgentRow | None:
    """Update a draft agent's config. Returns None if not found or not draft."""
    values: dict = {"config_blob": config_blob}
    if name is not None:
        values["name"] = name

    result = await session.execute(
        update(AgentRow)
        .where(AgentRow.id == agent_id, AgentRow.state == "draft")
        .values(**values)
        .returning(AgentRow)
    )
    return result.scalar_one_or_none()


async def publish_agent(
    session: AsyncSession,
    agent_id: UUID,
) -> AgentRow | None:
    """Publish a draft agent (freeze version). Returns None if not draft."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(AgentRow)
        .where(AgentRow.id == agent_id, AgentRow.state == "draft")
        .values(state="published", published_at=now)
        .returning(AgentRow)
    )
    return result.scalar_one_or_none()


async def get_latest_published(
    session: AsyncSession,
    project_id: UUID,
    name: str,
    environment: str,
) -> AgentRow | None:
    """Get the latest published version of a named agent."""
    result = await session.execute(
        select(AgentRow)
        .where(
            AgentRow.project_id == project_id,
            AgentRow.name == name,
            AgentRow.environment == environment,
            AgentRow.state == "published",
        )
        .order_by(AgentRow.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_next_version(
    session: AsyncSession,
    project_id: UUID,
    name: str,
) -> int:
    """Get the next version number for an agent name."""
    result = await session.execute(
        select(func.max(AgentRow.version)).where(
            AgentRow.project_id == project_id,
            AgentRow.name == name,
        )
    )
    current_max = result.scalar_one()
    return (current_max or 0) + 1


async def archive_agent(
    session: AsyncSession,
    agent_id: UUID,
) -> AgentRow | None:
    """Archive a published agent. Returns None if not published."""
    result = await session.execute(
        update(AgentRow)
        .where(AgentRow.id == agent_id, AgentRow.state == "published")
        .values(state="archived")
        .returning(AgentRow)
    )
    return result.scalar_one_or_none()


async def retire_agent(
    session: AsyncSession,
    agent_id: UUID,
) -> AgentRow | None:
    """Archive an agent from any state (delete semantics; history preserved)."""
    result = await session.execute(
        update(AgentRow)
        .where(AgentRow.id == agent_id)
        .values(state="archived")
        .returning(AgentRow)
    )
    return result.scalar_one_or_none()


async def unarchive_agent(
    session: AsyncSession,
    agent_id: UUID,
) -> AgentRow | None:
    """Restore an archived agent to published. Returns None if not archived."""
    result = await session.execute(
        update(AgentRow)
        .where(AgentRow.id == agent_id, AgentRow.state == "archived")
        .values(state="published")
        .returning(AgentRow)
    )
    return result.scalar_one_or_none()


async def list_versions(
    session: AsyncSession,
    project_id: UUID,
    name: str,
) -> list[AgentRow]:
    """List all versions of a named agent, newest first."""
    result = await session.execute(
        select(AgentRow)
        .where(
            AgentRow.project_id == project_id,
            AgentRow.name == name,
        )
        .order_by(AgentRow.version.desc())
    )
    return list(result.scalars().all())


async def archive_previous_published(
    session: AsyncSession,
    project_id: UUID,
    name: str,
    *,
    exclude_agent_id: UUID,
) -> list[UUID]:
    """Archive all published versions of a named agent except the given one.

    Returns the IDs of archived agents.
    """
    result = await session.execute(
        select(AgentRow.id).where(
            AgentRow.project_id == project_id,
            AgentRow.name == name,
            AgentRow.state == "published",
            AgentRow.id != exclude_agent_id,
        )
    )
    old_ids = list(result.scalars().all())
    if old_ids:
        await session.execute(
            update(AgentRow).where(AgentRow.id.in_(old_ids)).values(state="archived")
        )
    return old_ids


async def update_phone_number_routing(
    session: AsyncSession,
    old_agent_id: UUID,
    new_agent_id: UUID,
) -> int:
    """Update all phone numbers pointing to old_agent_id to new_agent_id.

    Returns count of updated phone numbers.
    """
    result = await session.execute(
        update(PhoneNumberRow)
        .where(
            PhoneNumberRow.routing_target_type == "agent",
            PhoneNumberRow.routing_target_id == old_agent_id,
        )
        .values(routing_target_id=new_agent_id)
    )
    return result.rowcount  # type: ignore[return-value]
