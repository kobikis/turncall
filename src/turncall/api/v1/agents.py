"""Agent management endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.audit import Audit
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.agents import (
    AgentConfigSchema,
    AgentResponse,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from turncall.auth import Auth, AuthContext, WriteAuth
from turncall.domain.enums import AuditAction, AuditResource
from turncall.domain.models import RetentionConfig
from turncall.services import audit_log, retention, skills
from turncall.services.audit_log import AuditTrail
from turncall.storage.repositories import (
    agent_repo,
    ambience_repo,
    knowledge_repo,
    skill_repo,
)

router = APIRouter(prefix="/agents", tags=["agents"])


async def _check_ambience(
    session: AsyncSession, project_id: UUID, config: AgentConfigSchema | None
) -> None:
    """A typo'd sound id fails here, at save, rather than silently on a call."""
    if config is None or config.ambience is None:
        return
    sound = config.ambience.sound
    if await ambience_repo.get_by_id(session, sound, project_id=project_id) is None:
        raise ApiError(
            status_code=422,
            code=ErrorCode.INVALID_AGENT_CONFIG,
            message=f"ambience.sound {sound} is not an ambience sound in this project",
            details={"field": "ambience.sound", "sound": str(sound)},
        )


async def _check_skills(
    session: AsyncSession, project_id: UUID, config: AgentConfigSchema | None
) -> None:
    """The skills an agent lists must be live in the project and bring no tool
    whose name is taken (#428): checked against the version each resolves to
    now, at save, rather than skipped or dropped on a call."""
    if config is None or not config.skills:
        return
    names = [ref.skill for ref in config.skills]
    # The lock a skill delete takes: a delete racing this save either lands
    # first and is seen below, or waits and then sees this agent.
    for name in sorted(set(names)):
        await skill_repo.lock_name(session, project_id, name)
    refusal = await skills.attachment_refusal(
        session, project_id, names, [tool.name for tool in config.tools]
    )
    if refusal is not None:
        raise ApiError(
            status_code=422,
            code=ErrorCode.INVALID_AGENT_CONFIG,
            message=refusal.message,
            details=refusal.details,
        )


def _refuse_inline_knowledge_bases(config: AgentConfigSchema | None) -> None:
    """A stored agent's KBs are its links — one source, never two that can
    disagree (#260). `knowledge_bases` is for an agent with no row."""
    if config is None or not config.knowledge_bases:
        return
    raise ApiError(
        status_code=422,
        code=ErrorCode.INVALID_AGENT_CONFIG,
        message=(
            "knowledge_bases is for an inline agent only; link a knowledge base "
            "to a stored agent with POST /v1/agents/{agent_id}/knowledge-bases"
        ),
        details={"field": "knowledge_bases"},
    )


# Changing what an agent keeps needs an admin key (#316): its `retention`
# overrides the project's, which only an admin sets, and decides when its
# calls' recordings, transcripts and events are deleted. A developer key may
# carry a retention over unchanged, never write a different one or make a
# different one apply.


async def _retention_before(
    session: AsyncSession, auth: AuthContext, names: set[str]
) -> dict[str, RetentionConfig] | None:
    """What retention each named agent's calls get before a change (its
    live version's); None for an admin key, which may change it."""
    if auth.can_admin():
        return None
    return await retention.applied_by_name(session, auth.project_id, names)


def _retention_forbidden(
    name: str, current: RetentionConfig, requested: RetentionConfig
) -> ApiError:
    return ApiError(
        status_code=403,
        code=ErrorCode.FORBIDDEN,
        message=(
            f"Changing the retention of agent {name!r} needs an admin key: it "
            "decides when the agent's calls' recordings, transcripts and "
            "events are deleted. Keep `retention` as it is, or use an admin key."
        ),
        details={
            "field": "retention",
            "agent": name,
            "current": current.as_payload(),
            "requested": requested.as_payload(),
        },
    )


def _refuse_written_retention(
    before: dict[str, RetentionConfig] | None,
    name: str,
    config_blob: dict[str, Any],
    *,
    own: dict[str, Any] | None = None,
) -> None:
    """A version written with a retention other than the one that applies to
    the agent (or, on an update, the one the version already had)."""
    if before is None:
        return
    written = retention.of_agent_config(config_blob)
    if written == before[name]:
        return
    if own is not None and written == retention.of_agent_config(own):
        return
    raise _retention_forbidden(name, before[name], written)


async def _refuse_applied_change(
    session: AsyncSession,
    auth: AuthContext,
    before: dict[str, RetentionConfig] | None,
) -> None:
    """A change that made a different retention apply to an agent's calls.
    What applies is the live version's, so a publish, rollback or delete can
    change it as well as a write. Checked after the change, in its
    transaction: the 403 rolls it back."""
    if before is None:
        return
    after = await retention.applied_by_name(session, auth.project_id, set(before))
    for name in sorted(before):
        if after[name] != before[name]:
            raise _retention_forbidden(name, before[name], after[name])


async def _promote(
    session: AsyncSession, audit: AuditTrail, name: str, project_id: UUID, to: UUID
) -> tuple[list[UUID], int]:
    """Archive the published versions of `name` other than `to`, and point
    their phone numbers at `to`: what a publish and a rollback both do.
    Each version archived and each number re-pointed is an audit entry of
    its own, after the request's primary one. The archived ids, and how
    many numbers moved."""
    archived_ids = await agent_repo.archive_previous_published(
        session, project_id, name, exclude_agent_id=to
    )
    moved = 0
    for old_id in archived_ids:
        audit.record(
            AuditResource.AGENT,
            old_id,
            AuditAction.ARCHIVED,
            before=audit_log.fields(state="published"),
            after=audit_log.fields(state="archived"),
        )
        for number_id in await agent_repo.update_phone_number_routing(
            session, old_id, to
        ):
            moved += 1
            audit.record(
                AuditResource.PHONE_NUMBER,
                number_id,
                AuditAction.UPDATED,
                before=audit_log.fields(routing_target_id=old_id),
                after=audit_log.fields(routing_target_id=to),
            )
    return archived_ids, moved


async def _with_kb_warnings(
    session: AsyncSession, rows: list[Any]
) -> list[AgentResponse]:
    """Agents as read, each warned about KB modes its pipeline drops (#254) —
    the links are not in the config, so they are loaded here, in one query."""
    modes = await knowledge_repo.kb_modes_by_agent(session, [r.id for r in rows])
    return [AgentResponse.from_row(r, modes.get(r.id, ())) for r in rows]


@router.post("", status_code=201)
async def create_agent(
    body: CreateAgentRequest,
    auth: WriteAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Create a new draft agent."""
    _refuse_inline_knowledge_bases(body.config)
    await _check_ambience(session, auth.project_id, body.config)
    await _check_skills(session, auth.project_id, body.config)
    before = await _retention_before(session, auth, {body.name})
    _refuse_written_retention(before, body.name, body.config.model_dump())
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
    await _refuse_applied_change(session, auth, before)
    audit.record(
        AuditResource.AGENT,
        row.id,
        AuditAction.CREATED,
        after=audit_log.agent_state(row),
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
    return ok((await _with_kb_warnings(session, [row]))[0])


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
        data=await _with_kb_warnings(session, rows),
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
    audit: Audit,
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

    _refuse_inline_knowledge_bases(body.config)
    await _check_ambience(session, auth.project_id, body.config)
    await _check_skills(session, auth.project_id, body.config)
    config_blob = body.config.model_dump() if body.config else existing.config_blob
    name = body.name or existing.name
    before = await _retention_before(session, auth, {existing.name, name})
    _refuse_written_retention(before, name, config_blob, own=existing.config_blob)
    with audit.change(
        AuditResource.AGENT,
        agent_id,
        AuditAction.UPDATED,
        audit_log.agent_state,
        existing,
    ) as change:
        row = await agent_repo.update_agent_config(
            session,
            agent_id,
            config_blob=config_blob,
            name=body.name,
        )
        if row is None:
            raise ConflictError("Agent was modified concurrently")
        await _refuse_applied_change(session, auth, before)
        change.to(row)

    return ok((await _with_kb_warnings(session, [row]))[0])


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Delete an agent (archives it — call history stays queryable)."""
    existing = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=auth.project_id
    )
    if existing is None:
        raise NotFoundError("Agent", str(agent_id))
    before = await _retention_before(session, auth, {existing.name})
    with audit.change(
        AuditResource.AGENT,
        agent_id,
        AuditAction.DELETED,
        audit_log.agent_state,
        existing,
    ) as change:
        row = await agent_repo.retire_agent(session, agent_id)
        await _refuse_applied_change(session, auth, before)
        if row is None:
            change.gone()
        else:
            change.to(row)
    return ok({"deleted": True})


@router.post("/{agent_id}/publish")
async def publish_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
    audit: Audit,
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

    before = await _retention_before(session, auth, {existing.name})
    with audit.change(
        AuditResource.AGENT,
        agent_id,
        AuditAction.PUBLISHED,
        audit_log.agent_state,
        existing,
    ) as change:
        row = await agent_repo.publish_agent(session, agent_id)
        if row is None:
            raise ConflictError(
                "Failed to publish - agent may have been modified concurrently"
            )
        change.to(row)

    # Auto-archive previous published versions + auto-promote phone numbers
    archived_ids, promoted_count = await _promote(
        session, audit, existing.name, auth.project_id, agent_id
    )
    await _refuse_applied_change(session, auth, before)

    if archived_ids:
        logger.info(
            "publish_auto_promoted",
            agent_name=existing.name,
            new_version=existing.version,
            archived_versions=len(archived_ids),
            phone_numbers_updated=promoted_count,
        )

    return ok((await _with_kb_warnings(session, [row]))[0])


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
    return ok(await _with_kb_warnings(session, versions))


@router.post("/{agent_id}/rollback")
async def rollback_agent(
    agent_id: UUID,
    auth: WriteAuth,
    session: DbSession,
    audit: Audit,
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

    before = await _retention_before(session, auth, {target.name})

    # Un-archive the target version
    with audit.change(
        AuditResource.AGENT,
        agent_id,
        AuditAction.ROLLED_BACK,
        audit_log.agent_state,
        target,
    ) as change:
        row = await agent_repo.unarchive_agent(session, agent_id)
        if row is None:
            raise ConflictError(
                "Failed to rollback - agent may have been modified concurrently"
            )
        change.to(row)

    # Archive the current published version(s) of the same name, and point
    # their phone numbers at this one
    archived_ids, promoted_count = await _promote(
        session, audit, target.name, auth.project_id, agent_id
    )
    await _refuse_applied_change(session, auth, before)

    logger.info(
        "agent_rollback",
        agent_name=target.name,
        rollback_to_version=target.version,
        archived_versions=len(archived_ids),
        phone_numbers_updated=promoted_count,
    )

    return ok((await _with_kb_warnings(session, [row]))[0])
