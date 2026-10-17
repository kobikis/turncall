"""The audit log, read (#317, ADR-0024).

Admin keys only: the log records every key's changes, the admin's own
included, and a developer or viewer key gets a 403. Nothing here, or anywhere
in the API, updates or deletes an entry.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from turncall.api.deps import DbSession
from turncall.api.errors import BadRequestError
from turncall.api.v1.schemas.audit_log import (
    AuditEntryResponse,
    decode_cursor,
    encode_cursor,
)
from turncall.auth import AdminAuth
from turncall.domain.enums import AuditAction, AuditResource
from turncall.storage.repositories import audit_repo

router = APIRouter(prefix="/audit-log", tags=["audit-log"])


def _utc(at: datetime | None) -> datetime | None:
    """A time given without a zone is UTC."""
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


@router.get("")
async def list_audit_log(
    auth: AdminAuth,
    session: DbSession,
    resource_type: AuditResource | None = None,
    resource_id: Annotated[str | None, Query(max_length=64)] = None,
    action: AuditAction | None = None,
    since: Annotated[
        datetime | None, Query(description="Entries at or after this time")
    ] = None,
    until: Annotated[
        datetime | None, Query(description="Entries before this time")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[
        str | None, Query(description="`next_cursor` from the previous page")
    ] = None,
) -> dict[str, Any]:
    """The project's audit log, newest first: each change made through the
    API to its agents, credentials, phone numbers, API keys and settings, the
    API key that made it, and what changed (secrets never recorded).

    Paged by cursor, not by page number: entries keep arriving at the head of
    the list, so a page number would shift under the reader. Pass
    `next_cursor` back as `cursor` for the next page; it is null on the last.
    Admin key only.
    """
    try:
        after = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise BadRequestError(str(exc), details={"field": "cursor"}) from exc
    rows = await audit_repo.list_entries(
        session,
        auth.project_id,
        resource_type=resource_type.value if resource_type else None,
        resource_id=resource_id,
        action=action.value if action else None,
        since=_utc(since),
        until=_utc(until),
        after=after,
        limit=limit + 1,
    )
    page = rows[:limit]
    more = len(rows) > limit
    return {
        "success": True,
        "data": [AuditEntryResponse.model_validate(r) for r in page],
        "next_cursor": encode_cursor(page[-1].created_at, page[-1].id)
        if more
        else None,
        "limit": limit,
    }
