"""Skill endpoints (#423): a project's versioned capabilities.

Each row is one version of a named skill, with the agent lifecycle: a create
is a draft (version 1 of a new name, or the next version of an existing one),
only a draft is edited, a publish makes it the name's live version and
archives the previous one, and a rollback restores an archived version as
live and archives the current one. A published or archived version never
changes. Every change is an audit log entry (`skill_state`).

A version is deleted on its own (#428), and not while any draft or published
agent version that reads the project's skills lists the skill's name —
whatever the version: an agent follows the live version, and publish and
rollback can make any version of the name the live one. The agents that read
a project's skills are its own, unless it is linked to a skill library, and
every linked project's when it is one (#434, `skill_library.readers`).
`GET /{name}/agents` lists those agents, each with its `project_id`.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.audit import Audit
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.skills import (
    CreateSkillRequest,
    SkillAgentResponse,
    SkillResponse,
    UpdateSkillRequest,
    pinned_version_id,
)
from turncall.auth import Auth, WriteAuth
from turncall.domain.config_secrets import MASK, keep_masked_tool_secrets
from turncall.domain.enums import AuditAction, AuditResource, SkillState
from turncall.domain.models import SKILL_NAME_MAX, SKILL_NAME_PATTERN
from turncall.services import audit_log, skill_library
from turncall.services.audit_log import AuditTrail
from turncall.storage.models import SkillRow
from turncall.storage.repositories import knowledge_repo, skill_repo

router = APIRouter(prefix="/skills", tags=["skills"])

MAX_PAGE_SIZE = 200
STATES = "|".join(s.value for s in SkillState)


async def _check_knowledge_bases(
    session: AsyncSession, project_id: UUID, kbs: list[dict[str, Any]] | None
) -> None:
    """A knowledge base outside the project fails here, at save."""
    for index, kb in enumerate(kbs or ()):
        kb_id = UUID(kb["knowledge_base_id"])
        found = await knowledge_repo.get_knowledge_base_by_id(
            session, kb_id, project_id=project_id
        )
        if found is None:
            raise ApiError(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    f"knowledge_bases[{index}] {kb_id} is not a knowledge base "
                    "in this project"
                ),
                details={
                    "field": f"knowledge_bases[{index}]",
                    "knowledge_base_id": str(kb_id),
                },
            )


async def _keep_secrets(
    session: AsyncSession,
    project_id: UUID,
    name: str,
    tools: list[dict[str, Any]],
    *,
    first: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A tool's secret sent back masked keeps the one its name holds: in
    `first` (the draft being edited), else the name's newest version that has
    it. A mask nothing fills is refused rather than stored."""
    if not any(t.get("webhook_secret") == MASK for t in tools):
        return tools
    earlier = await skill_repo.tools_by_version(session, project_id, name)
    kept, unfilled = keep_masked_tool_secrets(tools, [first or [], *earlier])
    if unfilled:
        index = unfilled[0]
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"tools[{index}] {tools[index]['name']!r} webhook_secret is masked "
                f"and no version of {name!r} holds a secret for that tool; "
                "send the secret itself"
            ),
            details={"field": f"tools[{index}].webhook_secret"},
        )
    return kept


async def _skill(session: AsyncSession, skill_id: UUID, project_id: UUID) -> SkillRow:
    """The project's skill version, or a 404 — another project's included."""
    row = await skill_repo.get_by_id(session, skill_id, project_id=project_id)
    if row is None:
        raise NotFoundError("Skill", str(skill_id))
    return row


def _not_in(row: SkillRow, action: str, expected: SkillState) -> ConflictError:
    return ConflictError(
        f"Can only {action} a {expected.value} skill version "
        f"(version {row.version} is {row.state})",
        details={"state": row.state, "version": row.version},
    )


async def _make_live(
    session: AsyncSession, audit: AuditTrail, row: SkillRow
) -> list[UUID]:
    """Archive the name's other published versions, each an entry of its own
    after the request's primary one: what a publish and a rollback share."""
    archived = await skill_repo.archive_other_published(
        session, row.project_id, row.name, keep=row.id
    )
    for old_id in archived:
        audit.record(
            AuditResource.SKILL,
            old_id,
            AuditAction.ARCHIVED,
            before=audit_log.fields(state=SkillState.PUBLISHED.value),
            after=audit_log.fields(state=SkillState.ARCHIVED.value),
        )
    return archived


@router.post("", status_code=201)
async def create_skill(
    body: CreateSkillRequest, auth: WriteAuth, session: DbSession, audit: Audit
) -> dict[str, Any]:
    """A new draft version of the named skill."""
    content = body.model_dump(mode="json")
    await _check_knowledge_bases(session, auth.project_id, content["knowledge_bases"])
    await skill_repo.lock_name(session, auth.project_id, body.name)
    tools = await _keep_secrets(session, auth.project_id, body.name, content["tools"])
    row = await skill_repo.create_skill(
        session,
        project_id=auth.project_id,
        name=body.name,
        version=await skill_repo.next_version(session, auth.project_id, body.name),
        description=content["description"],
        instructions=content["instructions"],
        tools=tools,
        knowledge_bases=content["knowledge_bases"],
    )
    audit.record(
        AuditResource.SKILL,
        row.id,
        AuditAction.CREATED,
        after=audit_log.skill_state(row),
    )
    return ok(SkillResponse.from_row(row))


@router.get("")
async def list_skills(
    auth: Auth,
    session: DbSession,
    name: Annotated[str | None, Query(pattern=SKILL_NAME_PATTERN)] = None,
    state: Annotated[str | None, Query(pattern=f"^({STATES})$")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> dict[str, Any]:
    """The project's skill versions, newest first; `name` lists one skill's
    versions, `state=published` the live ones."""
    rows = await skill_repo.list_skills(
        session,
        auth.project_id,
        name=name,
        state=state,
        limit=limit,
        offset=(page - 1) * limit,
    )
    total = await skill_repo.count_skills(
        session, auth.project_id, name=name, state=state
    )
    return paginated(
        data=[SkillResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{name}/agents")
async def list_skill_agents(
    name: Annotated[str, Path(max_length=SKILL_NAME_MAX, pattern=SKILL_NAME_PATTERN)],
    auth: Auth,
    session: DbSession,
) -> dict[str, Any]:
    """The draft and published agent versions listing the skill — the
    project's own, and every project linked to it as a skill library (#434),
    each with its `project_id` — with the skill version each resolves to
    now: the pinned one for an entry that pins (#426), else the live
    published one. A name the project has no version of is a 404."""
    if not await skill_repo.count_skills(session, auth.project_id, name=name):
        raise NotFoundError("Skill", name)
    live = await skill_repo.get_published(session, auth.project_id, name)
    readers = await skill_library.readers(session, auth.project_id)
    agents = await skill_repo.agents_listing(session, readers, name)
    pinned = (pinned_version_id(a.config_blob, name) for a in agents)
    pins = {pin for pin in pinned if pin is not None}
    versions = await skill_repo.get_by_ids(session, pins, project_id=auth.project_id)
    return ok([SkillAgentResponse.from_rows(a, name, live, versions) for a in agents])


@router.get("/{skill_id}")
async def get_skill(skill_id: UUID, auth: Auth, session: DbSession) -> dict[str, Any]:
    return ok(SkillResponse.from_row(await _skill(session, skill_id, auth.project_id)))


@router.put("/{skill_id}")
async def update_skill(
    skill_id: UUID,
    body: UpdateSkillRequest,
    auth: WriteAuth,
    session: DbSession,
    audit: Audit,
) -> dict[str, Any]:
    """Edit a draft. A published or archived version is a 409: create a new
    version instead."""
    existing = await _skill(session, skill_id, auth.project_id)
    if existing.state != SkillState.DRAFT.value:
        raise _not_in(existing, "edit", SkillState.DRAFT)
    values = body.values()
    if "tools" in values:
        values = {
            **values,
            "tools": await _keep_secrets(
                session,
                auth.project_id,
                existing.name,
                values["tools"],
                first=list(existing.tools),
            ),
        }
    await _check_knowledge_bases(
        session, auth.project_id, values.get("knowledge_bases")
    )
    with audit.change(
        AuditResource.SKILL,
        skill_id,
        AuditAction.UPDATED,
        audit_log.skill_state,
        existing,
    ) as change:
        row = (
            await skill_repo.update_draft(session, skill_id, values=values)
            if values
            else existing
        )
        if row is None:
            raise ConflictError("Skill was modified concurrently")
        change.to(row)
    return ok(SkillResponse.from_row(row))


@router.post("/{skill_id}/publish")
async def publish_skill(
    skill_id: UUID, auth: WriteAuth, session: DbSession, audit: Audit
) -> dict[str, Any]:
    """Make a draft the name's live version; the previous one is archived."""
    existing = await _skill(session, skill_id, auth.project_id)
    if existing.state != SkillState.DRAFT.value:
        raise _not_in(existing, "publish", SkillState.DRAFT)
    await skill_repo.lock_name(session, auth.project_id, existing.name)
    with audit.change(
        AuditResource.SKILL,
        skill_id,
        AuditAction.PUBLISHED,
        audit_log.skill_state,
        existing,
    ) as change:
        row = await skill_repo.publish(session, skill_id)
        if row is None:
            raise ConflictError("Skill was modified concurrently")
        change.to(row)
    archived = await _make_live(session, audit, row)
    logger.info(
        "skill_published",
        skill=row.name,
        version=row.version,
        archived_versions=len(archived),
    )
    return ok(SkillResponse.from_row(row))


@router.post("/{skill_id}/rollback")
async def rollback_skill(
    skill_id: UUID, auth: WriteAuth, session: DbSession, audit: Audit
) -> dict[str, Any]:
    """Restore an archived version as the name's live one; the current live
    version is archived."""
    target = await _skill(session, skill_id, auth.project_id)
    if target.state != SkillState.ARCHIVED.value:
        raise _not_in(target, "roll back to", SkillState.ARCHIVED)
    await skill_repo.lock_name(session, auth.project_id, target.name)
    with audit.change(
        AuditResource.SKILL,
        skill_id,
        AuditAction.ROLLED_BACK,
        audit_log.skill_state,
        target,
    ) as change:
        row = await skill_repo.restore(session, skill_id)
        if row is None:
            raise ConflictError("Skill was modified concurrently")
        change.to(row)
    archived = await _make_live(session, audit, row)
    logger.info(
        "skill_rolled_back",
        skill=row.name,
        version=row.version,
        archived_versions=len(archived),
    )
    return ok(SkillResponse.from_row(row))


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: UUID, auth: WriteAuth, session: DbSession, audit: Audit
) -> dict[str, Any]:
    """Delete one version. A 409 naming the agents, and their projects,
    while any draft or published agent version reading this project's
    skills lists the skill — a linked project's too (#434): remove it from
    their `skills` first."""
    existing = await _skill(session, skill_id, auth.project_id)
    # An agent save takes the same lock, so a save racing this delete either
    # lands first and is counted below, or reads the name without this version.
    await skill_repo.lock_name(session, auth.project_id, existing.name)
    readers = await skill_library.readers(session, auth.project_id)
    agents = await skill_repo.agents_listing(session, readers, existing.name)
    if agents:
        raise ConflictError(
            f"Skill '{existing.name}' is listed by {len(agents)} agent "
            f"version(s): {_named(agents, auth.project_id)}. Remove it from "
            "their skills first.",
            details={
                "skill": existing.name,
                "agents": [
                    {
                        "agent_id": str(a.id),
                        "project_id": str(a.project_id),
                        "name": a.name,
                        "version": a.version,
                        "state": a.state,
                    }
                    for a in agents
                ],
            },
        )
    with audit.change(
        AuditResource.SKILL,
        skill_id,
        AuditAction.DELETED,
        audit_log.skill_state,
        existing,
    ) as change:
        if not await skill_repo.delete(session, skill_id):
            raise ConflictError("Skill was modified concurrently")
        change.gone()
    logger.info("skill_deleted", skill=existing.name, version=existing.version)
    return ok({"deleted": True})


def _named(agents: list[Any], project_id: UUID) -> str:
    """Each agent version, and the project of one outside this project."""

    def named(a: Any) -> str:
        where = "" if a.project_id == project_id else f", project {a.project_id}"
        return f"{a.name} v{a.version} ({a.state}{where})"

    return ", ".join(named(a) for a in agents)
