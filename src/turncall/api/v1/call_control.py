"""Live call control API endpoints.

Runtime mutation of active calls. Uses the same shared primitives
as built-in tools (transfer_call, handoff_to_agent, etc.)
per PRD requirement 11.10.
"""

from collections.abc import Awaitable
from uuid import UUID

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.call_control import (
    ControlResultResponse,
    EndCallRequest,
    HandoffCallRequest,
    InjectContextRequest,
    SendDtmfRequest,
    TransferCallRequest,
)
from turncall.auth import WriteAuth
from turncall.services import call_control, live_call_channel
from turncall.services.call_control import ControlResult
from turncall.storage.repositories import call_repo

router = APIRouter(prefix="/calls", tags=["call-control"])


def _raise_if_failed(result: call_control.ControlResult) -> None:
    """Convert a failed ControlResult to an API error."""
    if not result.success:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=result.message,
            details=result.details,
        )


@router.post("/{call_id}/end")
async def end_call(
    call_id: UUID,
    body: EndCallRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """End an active call."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    result = await call_control.end_call(
        session,
        call_id,
        reason=body.reason,
    )
    _raise_if_failed(result)
    return ok(
        ControlResultResponse(
            success=result.success,
            message=result.message,
            details=result.details,
        )
    )


@router.post("/{call_id}/transfer")
async def transfer_call(
    call_id: UUID,
    body: TransferCallRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Transfer an active call to a human agent."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    result = await call_control.transfer_call(
        session,
        call_id,
        target_number=body.target_number,
        transfer_mode=body.transfer_mode,
        transfer_message=body.transfer_message,
        briefing=body.briefing,
        fallback_message=body.fallback_message,
        reason=body.reason,
    )
    _raise_if_failed(result)
    return ok(
        ControlResultResponse(
            success=result.success,
            message=result.message,
            details=result.details,
        )
    )


@router.post("/{call_id}/handoff")
async def handoff_call(
    call_id: UUID,
    body: HandoffCallRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Hand off an active call to another agent."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    result = await call_control.handoff_to_agent(
        session,
        call_id,
        target_agent_id=body.target_agent_id,
        reason=body.reason,
        context_payload=body.context_payload,
    )
    _raise_if_failed(result)
    return ok(
        ControlResultResponse(
            success=result.success,
            message=result.message,
            details=result.details,
        )
    )


@router.post("/{call_id}/dtmf")
async def send_dtmf(
    call_id: UUID,
    body: SendDtmfRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Send DTMF tones on an active call."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    result = await call_control.send_dtmf(
        session,
        call_id,
        digits=body.digits,
    )
    _raise_if_failed(result)
    return ok(
        ControlResultResponse(
            success=result.success,
            message=result.message,
            details=result.details,
        )
    )


async def _through_the_call(request: Awaitable[ControlResult]) -> ControlResult:
    """Await a request the live call itself must apply (#215): 409 when no
    task is running the call, 504 when it does not answer in time."""
    try:
        return await request
    except live_call_channel.CallNotLive:
        raise ApiError(
            status_code=409,
            code=ErrorCode.CALL_NOT_LIVE,
            message="The call is not live: nothing is running it",
        ) from None
    except live_call_channel.CallDidNotAcknowledge:
        raise ApiError(
            status_code=504,
            code=ErrorCode.CALL_DID_NOT_ACKNOWLEDGE,
            message=(
                "The call received the request but did not acknowledge it within "
                f"{live_call_channel.ACK_TIMEOUT_SECONDS:g}s; it may not have been applied"
            ),
        ) from None


@router.post(
    "/{call_id}/messages",
    responses={
        409: {"description": "The call is not live, or refused the message"},
        504: {"description": "The call did not acknowledge the message in time"},
    },
)
async def inject_message(
    call_id: UUID,
    body: InjectContextRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Add a message to a live call's LLM context, for the agent's next turn.

    Answered only once the call itself has applied it. Cascade calls only: a
    speech-to-speech call refuses it (409), since its model reads no context
    added after it starts.
    """
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    result = await _through_the_call(
        call_control.inject_context(
            session,
            call_id,
            message=body.message,
            role=body.role,
        )
    )
    _raise_if_failed(result)
    return ok(
        ControlResultResponse(
            success=result.success,
            message=result.message,
            details=result.details,
        )
    )
