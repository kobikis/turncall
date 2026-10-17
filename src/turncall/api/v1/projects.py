"""Project management endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.audit import Audit, PlatformAudit
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode, ForbiddenError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.projects import (
    CreateProjectRequest,
    ProjectLimits,
    ProjectRedaction,
    ProjectResponse,
    ProjectRetention,
)
from turncall.auth import AdminAuth, Auth, PlatformKey
from turncall.config import get_settings
from turncall.domain.enums import AuditAction, AuditResource
from turncall.services import audit_log, redaction
from turncall.storage.repositories import project_repo

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=201, dependencies=[PlatformKey])
async def create_project(
    body: CreateProjectRequest,
    session: DbSession,
    audit: PlatformAudit,
) -> dict:
    """Create a new project. Requires the platform credential (X-Platform-Key) —
    only the builder can provision projects (ADR-0011)."""
    row = await project_repo.create_project(session, name=body.name)
    audit.record(
        AuditResource.PROJECT,
        row.id,
        AuditAction.CREATED,
        after=audit_log.project_state(row),
        project_id=row.id,
    )
    return ok(ProjectResponse.from_row(row))


async def _project(session: AsyncSession, project_id: UUID) -> Any:
    """The project about to change; 404 if it is gone."""
    row = await project_repo.get_project_by_id(session, project_id)
    if row is None:
        raise NotFoundError("Project", str(project_id))
    return row


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
    audit: Audit,
) -> dict:
    """Set the project's limits (#206), replacing all of them: a field left
    out or null goes back to the platform default. Applies to the next call or
    message — calls already live keep their slots, and count toward the new
    limit. Admin key; a project can only set its own."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only set its own limits")
    with audit.change(
        AuditResource.PROJECT,
        project_id,
        AuditAction.LIMITS_SET,
        audit_log.project_state,
        await _project(session, project_id),
    ) as change:
        row = await project_repo.set_limits(
            session,
            project_id,
            limit_live_calls=body.live_calls,
            limit_outbound_calls_per_second=body.outbound_calls_per_second,
            limit_messages_per_minute=body.messages_per_minute,
        )
        if row is None:
            raise NotFoundError("Project", str(project_id))
        change.to(row)
    return ok(ProjectResponse.from_row(row))


@router.put("/{project_id}/redaction")
async def set_project_redaction(
    project_id: UUID,
    body: ProjectRedaction,
    auth: AdminAuth,
    session: DbSession,
    audit: Audit,
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
    with audit.change(
        AuditResource.PROJECT,
        project_id,
        AuditAction.REDACTION_SET,
        audit_log.project_state,
        await _project(session, project_id),
    ) as change:
        row = await project_repo.set_redaction(
            session, project_id, enabled=body.enabled, entities=body.entities
        )
        if row is None:
            raise NotFoundError("Project", str(project_id))
        change.to(row)
    # Committed here, before this process caches the policy; the commit
    # still writes the audit entry or is refused.
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


@router.put("/{project_id}/retention")
async def set_project_retention(
    project_id: UUID,
    body: ProjectRetention,
    auth: AdminAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Set how long the project keeps its calls' recordings, transcripts and
    events (#316, ADR-0024), replacing all three: a field left out or null
    keeps that kind for good. Counted in days from each call's end, and
    enforced by an hourly janitor, so data goes within the hour after it
    expires. Applies to calls already stored too: shortening it deletes what
    is now past it, lengthening it keeps what is not yet deleted. An agent's
    `retention` overrides it for that agent's calls. The call row itself is
    kept. Admin key; a project can only set its own."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only set its own retention")
    with audit.change(
        AuditResource.PROJECT,
        project_id,
        AuditAction.RETENTION_SET,
        audit_log.project_state,
        await _project(session, project_id),
    ) as change:
        row = await project_repo.set_retention(
            session,
            project_id,
            recordings_days=body.recordings_days,
            transcripts_days=body.transcripts_days,
            events_days=body.events_days,
        )
        if row is None:
            raise NotFoundError("Project", str(project_id))
        change.to(row)
    logger.info(
        "project_retention_set",
        project_id=str(project_id),
        recordings_days=body.recordings_days,
        transcripts_days=body.transcripts_days,
        events_days=body.events_days,
    )
    return ok(ProjectResponse.from_row(row))


@router.delete("/{project_id}")
async def delete_project(
    project_id: UUID,
    auth: AdminAuth,
    session: DbSession,
    audit: Audit,
) -> dict:
    """Soft-delete a project (ADR-0011). A project can only delete ITSELF (the
    path id must match the authenticated project) and only with an admin key.
    Auto-unbinds the project's Twilio numbers and sweeps its object storage,
    then marks the project deleted — its keys stop working immediately.
    IRREVERSIBLE for storage (recordings/KB files are purged)."""
    if auth.project_id != project_id:
        raise ForbiddenError("A project can only delete itself")
    row = await _project(session, project_id)

    from turncall.services.project_deletion import delete_project as run_delete

    # The entry outlives the project, its keys and the purge (ADR-0024).
    with audit.change(
        AuditResource.PROJECT,
        project_id,
        AuditAction.DELETED,
        audit_log.project_state,
        row,
    ) as change:
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
        change.to(row)  # soft-deleted in place: the same row, `deleted_at` set
    return ok({"deleted": True, "project_id": str(project_id)})
