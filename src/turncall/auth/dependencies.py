"""FastAPI auth dependencies for API key resolution."""

import secrets
from typing import Annotated

from fastapi import Depends, Header
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ForbiddenError, UnauthorizedError
from turncall.auth.api_keys import hash_api_key, legacy_hash_api_key
from turncall.auth.context import AuthContext
from turncall.config import get_settings
from turncall.domain.enums import ProjectRole
from turncall.storage.repositories import api_key_repo, project_repo


async def resolve_auth_context(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """Extract API key from Authorization header and resolve to AuthContext."""
    if not authorization:
        raise UnauthorizedError("Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise UnauthorizedError("Authorization header must use Bearer scheme")

    raw_key = authorization[7:]  # strip "Bearer "
    if not raw_key:
        raise UnauthorizedError("Empty API key")

    key_hash = hash_api_key(raw_key)
    api_key_row = await api_key_repo.get_api_key_by_hash(session, key_hash)

    if api_key_row is None:
        # Rollover: a key stored under the old unsalted SHA-256 scheme. Look it
        # up by the legacy hash and upgrade it in place to the peppered hash, so
        # every key migrates on first use (raw keys aren't stored to backfill).
        legacy_hash = legacy_hash_api_key(raw_key)
        api_key_row = await api_key_repo.get_api_key_by_hash(session, legacy_hash)
        if api_key_row is not None:
            api_key_row.key_hash = key_hash
            await session.flush()
            logger.info("auth_key_hash_upgraded", key_prefix=api_key_row.key_prefix)

    if api_key_row is None:
        logger.warning("auth_invalid_key", key_prefix=raw_key[:12])
        raise UnauthorizedError("Invalid API key")

    if api_key_row.revoked_at is not None:
        logger.warning(
            "auth_revoked_key",
            key_id=str(api_key_row.id),
            key_prefix=api_key_row.key_prefix,
        )
        raise UnauthorizedError("API key has been revoked")

    # A soft-deleted project's keys stop working — this is what disables the
    # whole project on delete (ADR-0011), without filtering deleted_at into
    # every query.
    if await project_repo.get_project_by_id(session, api_key_row.project_id) is None:
        logger.warning("auth_deleted_project", project_id=str(api_key_row.project_id))
        raise UnauthorizedError("Invalid API key")

    return AuthContext(
        project_id=api_key_row.project_id,
        api_key_id=api_key_row.id,
        role=ProjectRole(api_key_row.role),
        environment=api_key_row.environment,
    )


# Typed dependency shortcuts
Auth = Annotated[AuthContext, Depends(resolve_auth_context)]


def require_role(*roles: ProjectRole):  # type: ignore[no-untyped-def]
    """Dependency factory that enforces specific roles."""

    async def check_role(auth: Auth) -> AuthContext:
        if not auth.has_role(*roles):
            raise ForbiddenError(
                f"Requires one of: {', '.join(r.value for r in roles)}"
            )
        return auth

    return Depends(check_role)


AdminAuth = Annotated[AuthContext, require_role(ProjectRole.ADMIN)]
WriteAuth = Annotated[
    AuthContext, require_role(ProjectRole.ADMIN, ProjectRole.DEVELOPER)
]


async def require_platform_key(
    x_platform_key: Annotated[str | None, Header()] = None,
) -> None:
    """Gate the unauthenticated bootstrap endpoints (project + first-key creation)
    behind the single platform credential only the builder holds. TurnCall stays
    identity-free — this is a privileged-caller check, not a user. Fails closed:
    an unset PLATFORM_API_KEY rejects every caller (no prod-usable default)."""
    expected = get_settings().auth.platform_api_key
    if (
        not expected
        or not x_platform_key
        or not secrets.compare_digest(x_platform_key, expected)
    ):
        # Sole gate for the highest-privilege ops — make rejections observable
        # (matches auth_invalid_key/... above). Never log the submitted value.
        logger.warning("platform_key_rejected", has_header=x_platform_key is not None)
        raise UnauthorizedError("Missing or invalid platform credential")


# Route-decorator gate: dependencies=[PlatformKey] on the bootstrap endpoints.
PlatformKey = Depends(require_platform_key)
