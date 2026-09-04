"""Takeaway management endpoints (ADR-0013).

A Takeaway is a reusable post-call structured output: a named JSON schema an
LLM fills from the finished conversation. Agents attach takeaways via
analysis.takeaway_ids; results ship inside call.ended under analysis.takeaways.
"""

from uuid import UUID

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.errors import ConflictError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.takeaways import (
    CreateTakeawayRequest,
    TakeawayResponse,
    UpdateTakeawayRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.storage.repositories import takeaway_repo

router = APIRouter(prefix="/takeaways", tags=["takeaways"])


@router.post("", status_code=201)
async def create_takeaway(
    body: CreateTakeawayRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create a takeaway definition."""
    existing = await takeaway_repo.get_by_name(session, auth.project_id, body.name)
    if existing is not None:
        raise ConflictError(f"Takeaway {body.name!r} already exists")
    row = await takeaway_repo.create_takeaway(
        session,
        project_id=auth.project_id,
        name=body.name,
        schema=body.schema_,
        description=body.description,
        prompt=body.prompt,
        model=body.model,
    )
    await session.commit()
    return ok(TakeawayResponse.from_row(row))


@router.get("")
async def list_takeaways(auth: Auth, session: DbSession) -> dict:
    rows = await takeaway_repo.list_for_project(session, auth.project_id)
    return ok([TakeawayResponse.from_row(r) for r in rows])


@router.get("/{takeaway_id}")
async def get_takeaway(takeaway_id: UUID, auth: Auth, session: DbSession) -> dict:
    row = await takeaway_repo.get_by_id(session, takeaway_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("Takeaway", str(takeaway_id))
    return ok(TakeawayResponse.from_row(row))


@router.put("/{takeaway_id}")
async def update_takeaway(
    takeaway_id: UUID,
    body: UpdateTakeawayRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Update a takeaway (name is immutable — it keys results in payloads)."""
    row = await takeaway_repo.get_by_id(session, takeaway_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("Takeaway", str(takeaway_id))
    values = {
        k: v
        for k, v in {
            "description": body.description,
            "schema": body.schema_,
            "prompt": body.prompt,
            "model": body.model,
        }.items()
        if v is not None
    }
    updated = row
    if values:
        updated = await takeaway_repo.update_takeaway(
            session, takeaway_id, values=values
        )
        await session.commit()
    return ok(TakeawayResponse.from_row(updated))


@router.delete("/{takeaway_id}")
async def delete_takeaway(takeaway_id: UUID, auth: WriteAuth, session: DbSession) -> dict:
    """Delete a takeaway. Blocked while agents still attach it."""
    row = await takeaway_repo.get_by_id(session, takeaway_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("Takeaway", str(takeaway_id))
    refs = await takeaway_repo.count_agents_referencing(
        session, auth.project_id, takeaway_id
    )
    if refs:
        raise ConflictError(
            f"Takeaway is attached to {refs} agent(s) — detach it first",
            details={"agents_referencing": refs},
        )
    await takeaway_repo.delete_takeaway(session, takeaway_id)
    await session.commit()
    return ok({"deleted": True})
