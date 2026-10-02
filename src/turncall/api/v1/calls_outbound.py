"""Outbound call initiation endpoint."""

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.responses import ok
from turncall.api.v1.schemas.calls import CallResponse, CreateOutboundCallRequest
from turncall.auth import WriteAuth
from turncall.services.outbound_dial import place_outbound_call

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/outbound", status_code=201)
async def create_outbound_call(
    body: CreateOutboundCallRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create an outbound call via Twilio."""
    call = await place_outbound_call(
        session,
        project_id=auth.project_id,
        agent_id=body.agent_id,
        from_number_id=body.from_number_id,
        to_number=body.to_number,
        metadata=body.metadata,
    )
    return ok(CallResponse.from_row(call))
