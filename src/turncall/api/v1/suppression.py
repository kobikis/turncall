"""The suppression list: numbers a project never dials (#227).

Enforced in `services/outbound_dial`, the one path every outbound call takes,
so a listed number is refused however the dial was asked for. No consent
records: consent is the operator's legal record, not the platform's.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas import E164_PATTERN
from turncall.api.v1.schemas.suppression import (
    SuppressedNumberResponse,
    SuppressNumberRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.storage.repositories import suppression_repo

router = APIRouter(prefix="/suppression-list", tags=["suppression-list"])


@router.post("", status_code=201)
async def suppress_number(
    body: SuppressNumberRequest, auth: WriteAuth, session: DbSession
) -> dict:
    """Add a number. Adding one already listed returns the existing entry."""
    row = await suppression_repo.add(
        session, project_id=auth.project_id, number=body.number
    )
    await session.commit()
    return ok(SuppressedNumberResponse.from_row(row))


@router.get("")
async def list_suppressed_numbers(
    auth: Auth,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    number: Annotated[str | None, Query(pattern=E164_PATTERN)] = None,
) -> dict:
    """The list, newest first, paged; `number` finds one entry's id."""
    rows, total = await suppression_repo.list_for_project(
        session, auth.project_id, limit=limit, offset=(page - 1) * limit, number=number
    )
    return paginated(
        [SuppressedNumberResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.delete("/{entry_id}")
async def unsuppress_number(
    entry_id: UUID, auth: WriteAuth, session: DbSession
) -> dict:
    if not await suppression_repo.remove(
        session, project_id=auth.project_id, entry_id=entry_id
    ):
        raise NotFoundError("SuppressedNumber", str(entry_id))
    await session.commit()
    return ok({"deleted": True})
