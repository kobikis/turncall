"""Project management endpoints."""

from uuid import UUID

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode, ForbiddenError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.projects import (
    CreateProjectRequest,
    ProjectLimits,
    ProjectRedaction,
    ProjectResponse,
)
from turncall.auth import AdminAuth, Auth, PlatformKey
from turncall.config import get_settings
from turncall.services import redaction
from turncall.storage.repositories import project_repo

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=201, dependencies=[PlatformKey])
async def create_project(
    body: CreateProjectRequest,
    session: DbSession,
) -> dict:
    """Create a new project. Requires the platform credential (X-Platform-Key) —
    only the builder can provision projects (ADR-0011)."""
    row = await project_repo.create_project(session, name=body.name)
    return ok(ProjectResponse.from_row(row))


@router.get("/{project_id}")
async def get_project(
    project_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a project by ID. Must belong to the authenticated project."""
    if auth.project_id != project_id:
        raise ForbiddenError("Cannot access other projects")
    row = await project_repo.get_project_by_id(session, project_id)
    if row is None:
        raise NotFoundError("Project", str(project_id))
    return ok(ProjectResponse.from_row(row))


@router.put("/{project_id}/limits")
async def set_project_limits(
    project_id: UUID,
    body: ProjectLimits,
    auth: AdminAuth,
    session: DbSession,
) -> dict:
    """Set the project's limits (#206), replacing all of them: a field left
    out or null goes back to the platform default. Applies to the next call or
    message — calls already live keep their slots, and count toward the new
    limit. Admin key; a project can only set its own."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only set its own limits")
    row = await project_repo.set_limits(
        session,
        project_id,
        limit_live_calls=body.live_calls,
        limit_outbound_calls_per_second=body.outbound_calls_per_second,
        limit_messages_per_minute=body.messages_per_minute,
    )
    if row is None:
        raise NotFoundError("Project", str(project_id))
    return ok(ProjectResponse.from_row(row))


@router.put("/{project_id}/redaction")
async def set_project_redaction(
    project_id: UUID,
    body: ProjectRedaction,
    auth: AdminAuth,
    session: DbSession,
) -> dict:
    """Turn the project's redaction on or off and choose its entity types
    (#313, ADR-0024), replacing the whole setting: `enabled` is required, and
    `entities` left out or null is the default list. It applies to what is
    stored or sent from then on, a call already live included: at once on the
    server that took this request, and within 30 seconds on the others (each
    caches the setting that long). What was stored before is not rewritten.
    Admin key; a project can only set its own."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only set its own redaction")
    row = await project_repo.set_redaction(
        session, project_id, enabled=body.enabled, entities=body.entities
    )
    if row is None:
        raise NotFoundError("Project", str(project_id))
    await session.commit()
    policy = redaction.policy_of(body.enabled, body.entities)
    redaction.remember_policy(project_id, policy)
    if policy is not None:
        redaction.warm()  # in the background: the first call must not pay it
    logger.info(
        "project_redaction_set",
        project_id=str(project_id),
        enabled=body.enabled,
        entities=body.entities,
    )
    return ok(ProjectResponse.from_row(row))


@router.delete("/{project_id}")
async def delete_project(
    project_id: UUID,
    auth: AdminAuth,
    session: DbSession,
) -> dict:
    """Soft-delete a project (ADR-0011). A project can only delete ITSELF (the
    path id must match the authenticated project) and only with an admin key.
    Auto-unbinds the project's Twilio numbers and sweeps its object storage,
    then marks the project deleted — its keys stop working immediately.
    IRREVERSIBLE for storage (recordings/KB files are purged)."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only delete itself")
    if await project_repo.get_project_by_id(session, project_id) is None:
        raise NotFoundError("Project", str(project_id))

    from turncall.services.project_deletion import delete_project as run_delete

    try:
        await run_delete(session, get_settings(), project_id)
    except ApiError:
        raise
    except Exception as exc:
        logger.exception("project_delete_failed for {pid}", pid=str(project_id))
        raise ApiError(
            status_code=502,
            code=ErrorCode.INTERNAL_ERROR,
            message=(
                "Failed to clear the project's phone numbers — nothing was "
                "deleted; please retry."
            ),
        ) from exc

    return ok({"deleted": True, "project_id": str(project_id)})
