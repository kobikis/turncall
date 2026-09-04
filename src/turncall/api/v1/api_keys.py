"""API key management endpoints.

The first API key for a project is created without auth (bootstrap).
Subsequent operations require auth scoped to the project.
"""

from uuid import UUID

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.errors import ForbiddenError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
)
from turncall.auth import Auth, PlatformKey, WriteAuth
from turncall.auth.api_keys import generate_api_key
from turncall.storage.repositories import api_key_repo, project_repo

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("", status_code=201, dependencies=[PlatformKey])
async def create_api_key(
    project_id: UUID,
    body: CreateApiKeyRequest,
    session: DbSession,
) -> dict:
    """Create the first API key for a project. Requires the platform credential
    (X-Platform-Key) — only the builder bootstraps keys (ADR-0011).

    After the first key exists, use authenticated endpoints to manage keys.
    """
    # Check if project already has keys — if so, require auth
    existing_keys = await api_key_repo.list_api_keys_for_project(session, project_id)
    if existing_keys:
        raise ForbiddenError(
            "Project already has API keys. Use an authenticated request to create more."
        )

    project = await project_repo.get_project_by_id(session, project_id)
    if project is None:
        raise NotFoundError("Project", str(project_id))

    generated = generate_api_key()
    row = await api_key_repo.create_api_key(
        session,
        project_id=project_id,
        key_prefix=generated.key_prefix,
        key_hash=generated.key_hash,
        name=body.name,
        role=body.role.value,
        environment=body.environment,
    )

    return ok(
        ApiKeyCreatedResponse(
            id=row.id,
            project_id=row.project_id,
            key_prefix=row.key_prefix,
            raw_key=generated.raw_key,
            name=row.name,
            role=row.role,
            environment=row.environment,
            created_at=row.created_at,
        )
    )


@router.post("/authenticated", status_code=201)
async def create_api_key_authenticated(
    body: CreateApiKeyRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create an additional API key. Requires a write role (admin/developer);
    the new key's role may not exceed the caller's — otherwise a lower-privilege
    key could mint an admin key and escalate."""
    if body.role.rank > auth.role.rank:
        raise ForbiddenError(
            f"Cannot create a key with role '{body.role.value}' — "
            f"it exceeds your own role '{auth.role.value}'."
        )
    generated = generate_api_key()
    row = await api_key_repo.create_api_key(
        session,
        project_id=auth.project_id,
        key_prefix=generated.key_prefix,
        key_hash=generated.key_hash,
        name=body.name,
        role=body.role.value,
        environment=body.environment,
    )

    return ok(
        ApiKeyCreatedResponse(
            id=row.id,
            project_id=row.project_id,
            key_prefix=row.key_prefix,
            raw_key=generated.raw_key,
            name=row.name,
            role=row.role,
            environment=row.environment,
            created_at=row.created_at,
        )
    )


@router.get("")
async def list_api_keys(
    auth: Auth,
    session: DbSession,
) -> dict:
    """List API keys for the authenticated project (without secrets)."""
    rows = await api_key_repo.list_api_keys_for_project(session, auth.project_id)
    return ok([ApiKeyResponse.model_validate(r) for r in rows])


@router.delete("/{api_key_id}", status_code=200)
async def revoke_api_key(
    api_key_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Revoke an API key (must belong to the authenticated project)."""
    revoked = await api_key_repo.revoke_api_key(
        session, api_key_id, project_id=auth.project_id
    )
    if not revoked:
        raise NotFoundError("ApiKey", str(api_key_id))
    return ok({"revoked": True})
