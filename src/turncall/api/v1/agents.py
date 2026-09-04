"""Agent management endpoints."""

from uuid import UUID

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ConflictError, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.agents import (
    AgentResponse,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.storage.repositories import agent_repo

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("", status_code=201)
async def create_agent(
    body: CreateAgentRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create a new draft agent."""
    next_version = await agent_repo.get_next_version(
        session, auth.project_id, body.name
    )

    row = await agent_repo.create_agent(
        session,
        project_id=auth.project_id,
        name=body.name,
        environment=body.environment,
        version=next_version,
        config_blob=body.config.model_dump(),
    )

    return ok(AgentResponse.from_row(row))


@router.get("/{agent_id}")
async def get_agent(
    agent_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get an agent by ID."""
    row = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("Agent", str(agent_id))
    return ok(AgentResponse.from_row(row))


@router.get("")
async def list_agents(
    auth: Auth,
    session: DbSession,
    environment: str | None = None,
    state: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List agents for the authenticated project."""
    offset = (page - 1) * limit
    rows = await agent_repo.list_agents(
        session,
        auth.project_id,
        environment=environment,
        state=state,
        limit=limit,
        offset=offset,
    )
    total = await agent_repo.count_agents(
        session,
        auth.project_id,
        environment=environment,
        state=state,
    )
    return paginated(
        data=[AgentResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.put("/{agent_id}")
async def update_agent(
    agent_id: UUID,
    body: UpdateAgentRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Update a draft agent's config. Published agents cannot be updated."""
    existing = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("Agent", str(agent_id))

    if existing.state != "draft":
        raise ConflictError(
            "Cannot update a published agent. Create a new version instead.",
            details={"state": existing.state, "version": existing.version},
        )

    config_blob = body.config.model_dump() if body.config else existing.config_blob
    row = await agent_repo.update_agent_config(
        session,
        agent_id,
        config_blob=config_blob,
        name=body.name,
    )
    if row is None:
        raise ConflictError("Agent was modified concurrently")

    return ok(AgentResponse.from_row(row))


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Delete an agent (archives it — call history stays queryable)."""
    existing = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("Agent", str(agent_id))
    await agent_repo.retire_agent(session, agent_id)
    return ok({"deleted": True})


@router.post("/{agent_id}/publish")
async def publish_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Publish a draft agent. Freezes the version immutably."""
    existing = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("Agent", str(agent_id))

    if existing.state == "published":
        raise ConflictError(
            f"Agent is already published (version {existing.version})",
            details={"state": existing.state, "version": existing.version},
        )

    row = await agent_repo.publish_agent(session, agent_id)
    if row is None:
        raise ConflictError(
            "Failed to publish - agent may have been modified concurrently"
        )

    # Auto-archive previous published versions + auto-promote phone numbers
    archived_ids = await agent_repo.archive_previous_published(
        session,
        auth.project_id,
        existing.name,
        exclude_agent_id=agent_id,
    )
    promoted_count = 0
    for old_id in archived_ids:
        promoted_count += await agent_repo.update_phone_number_routing(
            session, old_id, agent_id
        )

    if archived_ids:
        logger.info(
            "publish_auto_promoted",
            agent_name=existing.name,
            new_version=existing.version,
            archived_versions=len(archived_ids),
            phone_numbers_updated=promoted_count,
        )

    return ok(AgentResponse.from_row(row))


@router.get("/{agent_id}/versions")
async def list_agent_versions(
    agent_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """List all versions of an agent (by name)."""
    agent = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if agent is None:
        raise NotFoundError("Agent", str(agent_id))

    versions = await agent_repo.list_versions(session, auth.project_id, agent.name)
    return ok([AgentResponse.from_row(v) for v in versions])


@router.post("/{agent_id}/rollback")
async def rollback_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Rollback to a previous version: un-archive it, archive current, update routing."""
    target = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if target is None:
        raise NotFoundError("Agent", str(agent_id))

    if target.state != "archived":
        raise ConflictError(
            f"Can only rollback to an archived version (current state: {target.state})",
            details={"state": target.state, "version": target.version},
        )

    # Archive current published version(s) of same name
    archived_ids = await agent_repo.archive_previous_published(
        session,
        auth.project_id,
        target.name,
        exclude_agent_id=agent_id,
    )

    # Un-archive the target version
    row = await agent_repo.unarchive_agent(session, agent_id)
    if row is None:
        raise ConflictError(
            "Failed to rollback - agent may have been modified concurrently"
        )

    # Update phone numbers from old published version(s) to this one
    promoted_count = 0
    for old_id in archived_ids:
        promoted_count += await agent_repo.update_phone_number_routing(
            session, old_id, agent_id
        )

    logger.info(
        "agent_rollback",
        agent_name=target.name,
        rollback_to_version=target.version,
        archived_versions=len(archived_ids),
        phone_numbers_updated=promoted_count,
    )

    return ok(AgentResponse.from_row(row))
