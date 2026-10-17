"""Audit log repository (#317, ADR-0024): append, read, and the retention delete.

Nothing here updates an entry, and the only delete is `delete_older_than`, the
audit retention janitor's. The table's trigger refuses anything else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, insert, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.storage.models import AuditEntryRow

# The janitor's transaction says it is the janitor; the trigger lets only
# such a transaction delete (migration b3d7f1a9c5e2).
RETENTION_SETTING = "turncall.audit_log_retention"


async def append(session: AsyncSession, **values: Any) -> None:
    """Insert one entry in the caller's transaction. A plain INSERT rather than
    `session.add`, so it is written exactly when this is awaited, before the
    change's own statements can be rolled back without it."""
    await session.execute(insert(AuditEntryRow).values(**values))


async def list_entries(
    session: AsyncSession,
    project_id: UUID,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    after: tuple[datetime, UUID] | None = None,
    limit: int,
) -> list[AuditEntryRow]:
    """A project's entries, newest first. `after` is the keyset cursor: the
    (created_at, id) of the last entry of the previous page."""
    query = select(AuditEntryRow).where(AuditEntryRow.project_id == project_id)
    if resource_type is not None:
        query = query.where(AuditEntryRow.resource_type == resource_type)
    if resource_id is not None:
        query = query.where(AuditEntryRow.resource_id == resource_id)
    if action is not None:
        query = query.where(AuditEntryRow.action == action)
    if since is not None:
        query = query.where(AuditEntryRow.created_at >= since)
    if until is not None:
        query = query.where(AuditEntryRow.created_at < until)
    if after is not None:
        at, entry_id = after
        query = query.where(
            or_(
                AuditEntryRow.created_at < at,
                and_(AuditEntryRow.created_at == at, AuditEntryRow.id < entry_id),
            )
        )
    query = query.order_by(
        AuditEntryRow.created_at.desc(), AuditEntryRow.id.desc()
    ).limit(limit)
    return list((await session.execute(query)).scalars().all())


async def delete_older_than(
    session: AsyncSession, cutoff: datetime, *, batch: int
) -> int:
    """Delete up to `batch` entries made before `cutoff`, every project's, in
    the caller's transaction, marked as the janitor's. The count deleted."""
    await session.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": RETENTION_SETTING}
    )
    oldest = (
        select(AuditEntryRow.id)
        .where(AuditEntryRow.created_at < cutoff)
        .order_by(AuditEntryRow.created_at)
        .limit(batch)
    )
    result = await session.execute(
        delete(AuditEntryRow).where(AuditEntryRow.id.in_(oldest))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
