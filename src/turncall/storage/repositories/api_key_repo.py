"""API key repository - data access for API keys."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import ApiKeyRow


async def create_api_key(
    session: AsyncSession,
    *,
    project_id: UUID,
    key_prefix: str,
    key_hash: str,
    name: str,
    role: str,
    environment: str | None = None,
) -> ApiKeyRow:
    """Store a new API key record."""
    row = ApiKeyRow(
        project_id=project_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        name=name,
        role=role,
        environment=environment,
    )
    session.add(row)
    await session.flush()
    return row


async def get_api_key_by_hash(session: AsyncSession, key_hash: str) -> ApiKeyRow | None:
    """Look up an API key by its hash."""
    result = await session.execute(
        select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash)
    )
    return result.scalar_one_or_none()


async def get_api_key_by_prefix(
    session: AsyncSession, key_prefix: str
) -> ApiKeyRow | None:
    """Look up an API key by prefix (for display/debugging only)."""
    result = await session.execute(
        select(ApiKeyRow).where(ApiKeyRow.key_prefix == key_prefix)
    )
    return result.scalar_one_or_none()


async def list_api_keys_for_project(
    session: AsyncSession,
    project_id: UUID,
    *,
    include_revoked: bool = False,
) -> list[ApiKeyRow]:
    """List API keys for a project."""
    query = select(ApiKeyRow).where(ApiKeyRow.project_id == project_id)
    if not include_revoked:
        query = query.where(ApiKeyRow.revoked_at.is_(None))
    query = query.order_by(ApiKeyRow.created_at.desc())
    result = await session.execute(query)
    return list(result.scalars().all())


async def revoke_api_key(
    session: AsyncSession,
    api_key_id: UUID,
    *,
    project_id: UUID | None = None,
) -> bool:
    """Revoke an API key. Returns True if a key was revoked. When project_id is
    given, only revokes a key belonging to that project — without it any project
    could revoke another's keys by id."""
    query = update(ApiKeyRow).where(
        ApiKeyRow.id == api_key_id, ApiKeyRow.revoked_at.is_(None)
    )
    if project_id is not None:
        query = query.where(ApiKeyRow.project_id == project_id)
    result = await session.execute(query.values(revoked_at=datetime.now(UTC)))
    return result.rowcount > 0  # type: ignore[union-attr]
