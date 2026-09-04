"""Outbound call initiation endpoint."""

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.calls import CallResponse, CreateOutboundCallRequest
from turncall.auth import WriteAuth
from turncall.config import get_settings
from turncall.domain.enums import CallDirection, CallEventType
from turncall.storage.repositories import agent_repo, call_repo, phone_number_repo

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/outbound", status_code=201)
async def create_outbound_call(
    body: CreateOutboundCallRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create an outbound call via Twilio."""
    settings = get_settings()

    if not settings.twilio.account_sid or not settings.twilio.auth_token:
        raise ApiError(
            status_code=503,
            code=ErrorCode.INTERNAL_ERROR,
            message="Twilio credentials not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)",
        )

    # Verify agent exists
    agent = await agent_repo.get_agent_by_id(
        session, body.agent_id, project_id=auth.project_id
    )
    if agent is None:
        raise NotFoundError("Agent", str(body.agent_id))

    # Verify phone number belongs to project
    phone_number = await phone_number_repo.get_by_id(
        session, body.from_number_id, project_id=auth.project_id
    )
    if phone_number is None:
        raise NotFoundError("PhoneNumber", str(body.from_number_id))

    # Create internal call record
    call = await call_repo.create_call(
        session,
        project_id=auth.project_id,
        direction=CallDirection.OUTBOUND,
        from_number=phone_number.e164_number,
        to_number=body.to_number,
        active_agent_id=agent.id,
        metadata_json=body.metadata,
    )

    # Build webhook URLs from the configured public base — NEVER the client Host
    # header, which a caller can spoof to make Twilio fetch call TwiML from an
    # attacker-controlled server using our Twilio credentials.
    base_url = (settings.server.public_base_url or "").rstrip("/")
    if not base_url:
        raise ConflictError(
            "PUBLIC_BASE_URL must be configured to place outbound calls "
            "(Twilio needs a reachable webhook URL)."
        )
    voice_url = f"{base_url}/webhooks/twilio/voice/outbound"
    status_url = f"{base_url}/webhooks/twilio/status"

    # Initiate call via Twilio (env var credentials)
    from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

    adapter = TwilioAdapter(settings.twilio.account_sid, settings.twilio.auth_token)
    provider_call_sid = await adapter.initiate_outbound_call(
        from_number=phone_number.e164_number,
        to_number=body.to_number,
        webhook_url=voice_url,
        status_callback_url=status_url,
    )

    # Update call with provider SID
    call = await call_repo.update_call_status(
        session,
        call.id,
        status="initiated",
        provider_call_sid=provider_call_sid,
    )

    # Create event
    seq = await call_repo.get_next_sequence_number(session, call.id)  # type: ignore[union-attr]
    await call_repo.create_call_event(
        session,
        call_id=call.id,  # type: ignore[union-attr]
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": provider_call_sid,
            "from_number": phone_number.e164_number,
            "to_number": body.to_number,
            "agent_id": str(agent.id),
            "direction": "outbound",
        },
        sequence_number=seq,
    )

    logger.info(
        "outbound_call_created",
        call_id=str(call.id),  # type: ignore[union-attr]
        provider_call_sid=provider_call_sid,
    )

    return ok(CallResponse.from_row(call))
