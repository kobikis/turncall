"""Twilio webhook handlers for inbound calls, status callbacks, and recordings."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import Response
from loguru import logger

from turncall.api.deps import DbSession
from turncall.config import get_settings
from turncall.domain.call_state import TWILIO_STATUS_MAP, is_valid_transition
from turncall.domain.enums import CallDirection, CallEventType, CallStatus
from turncall.storage.repositories import (
    agent_repo,
    call_repo,
    phone_number_repo,
)
from turncall.webhooks.twilio_signature import validate_twilio_signature

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio-webhooks"])

TWIML_CONTENT_TYPE = "application/xml"


def _public_url(request: Request) -> str:
    """Rebuild the externally-visible URL Twilio signed.

    Behind a tunnel/proxy (ngrok, load balancer) the container sees plain http
    and an internal host, but Twilio signed the public https URL — so honor
    X-Forwarded-Proto/Host, matching how media-stream URLs are built elsewhere.
    Header values may be a comma-separated proxy chain; take the first.
    """
    fwd_proto = request.headers.get("x-forwarded-proto")
    scheme = fwd_proto.split(",")[0].strip() if fwd_proto else request.url.scheme
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host_header:
        return str(request.url)
    host = host_header.split(",")[0].strip()
    url = f"{scheme}://{host}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _validate_twilio_request(
    request: Request,
    signature: str,
    params: dict[str, str],
) -> bool:
    """Validate Twilio signature using env var credentials."""
    settings = get_settings()
    if not settings.twilio.auth_token:
        return True  # Skip validation if no auth token configured (dev mode)
    url = _public_url(request)
    return validate_twilio_signature(settings.twilio.auth_token, signature, url, params)


@router.post("/sms/inbound")
async def inbound_sms_webhook(
    request: Request,
    session: DbSession,
    message_sid: str = Form(alias="MessageSid"),
    from_number: str = Form(alias="From"),
    to_number: str = Form(alias="To"),
    body: str = Form(alias="Body"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Handle inbound Twilio SMS webhook.

    Resolves the phone number to an agent, processes the message
    through the chat service, and returns a TwiML reply.
    """
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        logger.warning("twilio_sms_invalid_signature")
        return Response(status_code=403)

    # Normalize phone numbers (Twilio may URL-encode '+' as space)
    to_number = to_number.strip()
    from_number = from_number.strip()
    if not to_number.startswith("+"):
        to_number = f"+{to_number}"
    if not from_number.startswith("+"):
        from_number = f"+{from_number}"

    logger.info(
        "twilio_inbound_sms",
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body_length=len(body),
    )

    # Verify the phone number exists and has SMS enabled
    phone_number = await phone_number_repo.get_by_e164(session, to_number)
    if phone_number is None:
        logger.warning("twilio_sms_unknown_number", to_number=to_number)
        msg = "This number is not configured for SMS."
        return Response(
            content=f"<Response><Message>{msg}</Message></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    if not phone_number.sms_enabled:
        logger.warning("twilio_sms_not_enabled", to_number=to_number)
        return Response(
            content="<Response><Message>SMS is not enabled on this number.</Message></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    try:
        from turncall.services.sms_chat import handle_inbound_sms

        result = await handle_inbound_sms(
            session,
            from_number=from_number,
            to_number=to_number,
            body=body,
            provider_message_sid=message_sid,
        )

        logger.info(
            "twilio_sms_reply_sent",
            session_id=str(result.session_id),
            is_new_session=result.is_new_session,
        )

        # Return TwiML with the agent's reply
        # XML-escape the reply text to prevent injection
        import xml.sax.saxutils

        escaped_reply = xml.sax.saxutils.escape(result.reply_text)
        return Response(
            content=f"<Response><Message>{escaped_reply}</Message></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )
    except ValueError as exc:
        logger.error("twilio_sms_error", error=str(exc))
        err_msg = "Sorry, I'm unable to process your message right now."
        return Response(
            content=f"<Response><Message>{err_msg}</Message></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )
    except Exception:
        logger.exception("twilio_sms_unexpected_error")
        err_msg = "An error occurred. Please try again later."
        return Response(
            content=f"<Response><Message>{err_msg}</Message></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )


@router.post("/voice/inbound")
async def inbound_voice_webhook(
    request: Request,
    session: DbSession,
    call_sid: str = Form(alias="CallSid"),
    from_number: str = Form(alias="From"),
    to_number: str = Form(alias="To"),
    call_status: str = Form(alias="CallStatus"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Handle inbound Twilio voice webhook.

    Validates Twilio signature before processing.

    Resolves the phone number to an agent, creates a call record,
    and returns TwiML to start a Media Stream.
    """
    # Validate Twilio signature
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        logger.warning("twilio_invalid_signature")
        return Response(status_code=403)

    logger.info(
        "twilio_inbound_call",
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        status=call_status,
    )

    # Resolve phone number to routing target
    phone_number = await phone_number_repo.get_by_e164(session, to_number)
    if phone_number is None:
        logger.warning("twilio_inbound_unknown_number", to_number=to_number)
        return Response(
            content="<Response><Say>This number is not configured.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    # Resolve agent — either static or dynamic via server URL
    agent = None
    dynamic_config = None
    template_variables: dict[str, str] = {}
    request_metadata: dict = {}
    knowledge_context: str | None = None

    if phone_number.routing_target_type == "webhook" and phone_number.server_url:
        # Dynamic agent resolution via call-init server event
        from uuid import uuid4

        from turncall.events.server_events import send_call_init
        from turncall.services.call_init_resolver import resolve_call_init

        temp_call_id = str(uuid4())
        response = await send_call_init(
            phone_number.server_url,
            call_id=temp_call_id,
            from_number=from_number,
            to_number=to_number,
            call_sid=call_sid,
            transport_type="inboundPhoneCall",
            secret=phone_number.server_url_secret,
        )
        if response.success and response.data:
            result = await resolve_call_init(session, response.data)
            agent = result.agent
            dynamic_config = result.dynamic_config
            template_variables = result.template_variables
            request_metadata = result.metadata
            knowledge_context = result.knowledge_context

        if agent is None and dynamic_config is None:
            logger.warning(
                "twilio_inbound_server_url_no_agent",
                server_url=phone_number.server_url,
            )
            return Response(
                content=(
                    "<Response><Say>Unable to resolve agent.</Say><Hangup/></Response>"
                ),
                media_type=TWIML_CONTENT_TYPE,
            )

    elif phone_number.routing_target_type == "agent" and phone_number.routing_target_id:
        # A/B weighted routing: pick agent by caller number hash
        if phone_number.routing_weights:
            from turncall.services.weighted_routing import pick_agent_by_weight

            selected_id = pick_agent_by_weight(
                phone_number.routing_weights, from_number
            )
            agent = await agent_repo.get_agent_by_id(session, selected_id)
            logger.info(
                "ab_routing_selected",
                phone_number=to_number,
                caller=from_number,
                selected_agent_id=str(selected_id),
            )
        else:
            agent = await agent_repo.get_agent_by_id(
                session, phone_number.routing_target_id
            )

    if agent is None and dynamic_config is None:
        logger.warning(
            "twilio_inbound_no_agent",
            routing_target_type=phone_number.routing_target_type,
            routing_target_id=str(phone_number.routing_target_id),
        )
        return Response(
            content="<Response><Say>This agent is not available.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    # Determine agent ID for the call record
    agent_id_for_call = agent.id if agent else None

    # Create internal call record
    call_metadata: dict = {
        **({"dynamic_config": dynamic_config} if dynamic_config else {}),
        **({"template_variables": template_variables} if template_variables else {}),
        **({"metadata": request_metadata} if request_metadata else {}),
        **({"knowledge_context": knowledge_context} if knowledge_context else {}),
    }
    call = await call_repo.create_call(
        session,
        project_id=phone_number.project_id,
        direction=CallDirection.INBOUND,
        from_number=from_number,
        to_number=to_number,
        active_agent_id=agent_id_for_call,
        provider_call_sid=call_sid,
        metadata_json=call_metadata,
    )

    # Fire call.initializing + call.started events (DB + webhook)
    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call.id,
        project_id=phone_number.project_id,
        event_type=CallEventType.CALL_INITIALIZING,
        payload={
            "from_number": from_number,
            "to_number": to_number,
            "direction": "inbound",
            "transport": "twilio",
        },
    )

    await emit_call_event(
        session,
        call_id=call.id,
        project_id=phone_number.project_id,
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": call_sid,
            "from_number": from_number,
            "to_number": to_number,
            # agent_id is carried in the envelope (ADR-0007), not the payload.
            "agent_name": agent.name if agent else "dynamic",
            "dynamic_config": bool(dynamic_config),
        },
    )

    # Generate TwiML to start Media Stream
    settings = get_settings()
    host = request.headers.get("host", "localhost:8000")
    # Use wss:// when behind HTTPS (ngrok, production) — detect via X-Forwarded-Proto
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = forwarded_proto == "https" or settings.is_production
    scheme = "wss" if is_https else "ws"
    ws_url = f"{scheme}://{host}/ws/media-stream"

    # Simplified TwiML — only pass call_id.
    # Everything else resolved server-side from the call record.
    http_scheme = "https" if is_https else "http"
    status_cb = f"{http_scheme}://{host}/webhooks/twilio/stream-status"
    twiml = (
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" statusCallback="{status_cb}">'
        f'<Parameter name="callId" value="{call.id}" />'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )

    logger.info(
        "twilio_inbound_connected",
        call_id=str(call.id),
        agent_id=str(agent.id),
        call_sid=call_sid,
    )

    return Response(content=twiml, media_type=TWIML_CONTENT_TYPE)


@router.post("/voice/outbound")
async def outbound_voice_webhook(
    request: Request,
    session: DbSession,
    call_sid: str = Form(alias="CallSid"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Handle outbound Twilio voice webhook.

    Resolves the agent from the call record (by CallSid),
    not by phone number. This is the key difference from inbound.
    """
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    logger.info("twilio_outbound_call", call_sid=call_sid)

    # Look up the call record by provider SID
    call = await call_repo.get_call_by_provider_sid(session, call_sid)
    if call is None:
        logger.warning("twilio_outbound_unknown_call", call_sid=call_sid)
        return Response(
            content="<Response><Say>Call not found.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    # Load agent from the call record
    agent = None
    if call.active_agent_id:
        agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)

    if agent is None:
        return Response(
            content="<Response><Say>Agent not available.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    # Generate TwiML to start Media Stream
    settings = get_settings()
    host = request.headers.get("host", "localhost:8000")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = forwarded_proto == "https" or settings.is_production
    scheme = "wss" if is_https else "ws"
    ws_url = f"{scheme}://{host}/ws/media-stream"

    http_scheme = "https" if is_https else "http"
    status_cb = f"{http_scheme}://{host}/webhooks/twilio/stream-status"
    twiml = (
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" statusCallback="{status_cb}">'
        f'<Parameter name="callId" value="{call.id}" />'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )

    logger.info(
        "twilio_outbound_connected",
        call_id=str(call.id),
        agent_id=str(agent.id),
    )

    return Response(content=twiml, media_type=TWIML_CONTENT_TYPE)


@router.post("/status")
async def status_callback(
    request: Request,
    session: DbSession,
    call_sid: str = Form(alias="CallSid"),
    call_status: str = Form(alias="CallStatus"),
    call_duration: str | None = Form(default=None, alias="CallDuration"),
    timestamp: str | None = Form(default=None, alias="Timestamp"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Handle Twilio status callback. Updates internal call state."""
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    logger.info(
        "twilio_status_callback",
        call_sid=call_sid,
        status=call_status,
        duration=call_duration,
    )

    call = await call_repo.get_call_by_provider_sid(session, call_sid)
    if call is None:
        logger.warning("twilio_status_unknown_call", call_sid=call_sid)
        return Response(status_code=204)

    # Map Twilio status to internal status
    new_status = TWILIO_STATUS_MAP.get(call_status)
    if new_status is None:
        logger.warning(
            "twilio_status_unmapped", call_sid=call_sid, twilio_status=call_status
        )
        return Response(status_code=204)

    current_status = CallStatus(call.status)
    if not is_valid_transition(current_status, new_status):
        logger.warning(
            "twilio_status_invalid_transition",
            call_sid=call_sid,
            current=current_status,
            target=new_status,
        )
        return Response(status_code=204)

    # Build update values
    update_kwargs: dict = {"status": new_status.value}

    if new_status == CallStatus.IN_PROGRESS and call.started_at is None:
        update_kwargs["started_at"] = datetime.now(UTC)

    if new_status in {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
    }:
        update_kwargs["ended_at"] = datetime.now(UTC)
        if call_duration:
            update_kwargs["duration_ms"] = int(call_duration) * 1000

    await call_repo.update_call_status(session, call.id, **update_kwargs)

    # Create event + dispatch webhook (except call.ended which is dispatched
    # by post-call trigger with full transcript/analysis)
    from turncall.events.emit import emit_call_event

    event_type = {
        CallStatus.COMPLETED: CallEventType.CALL_ENDED,
        CallStatus.FAILED: CallEventType.CALL_FAILED,
    }.get(new_status, CallEventType.CALL_STARTED)

    status_payload = {
        "provider_call_sid": call_sid,
        "twilio_status": call_status,
        "internal_status": new_status.value,
        "from_number": call.from_number,
        "to_number": call.to_number,
    }

    if new_status == CallStatus.COMPLETED:
        # DB event only — comprehensive webhook dispatched by post-call trigger
        seq = await call_repo.get_next_sequence_number(session, call.id)
        await call_repo.create_call_event(
            session,
            call_id=call.id,
            event_type=event_type,
            payload=status_payload,
            sequence_number=seq,
        )
    else:
        # Non-completed statuses: dispatch webhook immediately
        await emit_call_event(
            session,
            call_id=call.id,
            project_id=call.project_id,
            event_type=event_type,
            payload=status_payload,
        )

    # Trigger post-call processing when call completes
    if new_status == CallStatus.COMPLETED and call.active_agent_id:
        try:
            agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
            if agent:
                from turncall.services.call_analysis_trigger import (
                    trigger_post_call_analysis,
                )
                from turncall.storage.database import get_session_factory

                trigger_post_call_analysis(
                    get_session_factory(),
                    call.id,
                    call.project_id,
                    agent.config_blob,
                )
        except Exception:
            logger.exception("analysis_trigger_error", call_id=str(call.id))

    return Response(status_code=204)


@router.post("/recording")
async def recording_callback(
    request: Request,
    session: DbSession,
    call_sid: str = Form(alias="CallSid"),
    recording_sid: str = Form(alias="RecordingSid"),
    recording_url: str = Form(alias="RecordingUrl"),
    recording_status: str = Form(alias="RecordingStatus"),
    recording_duration: str | None = Form(default=None, alias="RecordingDuration"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Handle Twilio recording callback."""
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    logger.info(
        "twilio_recording_callback",
        call_sid=call_sid,
        recording_sid=recording_sid,
        status=recording_status,
    )

    call = await call_repo.get_call_by_provider_sid(session, call_sid)
    if call is None:
        logger.warning("twilio_recording_unknown_call", call_sid=call_sid)
        return Response(status_code=204)

    # Download recording from Twilio and store to local/S3
    stored_url: str | None = None
    if recording_status == "completed":
        try:
            from turncall.services.recording_storage import (
                download_and_store_recording,
            )

            stored_url = await download_and_store_recording(
                recording_url, call.id, recording_sid
            )
            await call_repo.update_call_recording_url(session, call.id, stored_url)
        except Exception:
            logger.exception(
                "recording_storage_error",
                call_id=str(call.id),
                recording_sid=recording_sid,
            )

    # Create recording event + dispatch webhook
    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call.id,
        project_id=call.project_id,
        event_type=CallEventType.RECORDING_READY,
        payload={
            "recording_sid": recording_sid,
            "recording_url": stored_url or recording_url,
            "twilio_recording_url": recording_url,
            "recording_status": recording_status,
            "recording_duration": recording_duration,
        },
    )

    return Response(status_code=204)


@router.post("/stream-status")
async def stream_status_callback(
    request: Request,
) -> Response:
    """Handle Twilio Media Stream status callback.

    Notifies when the audio stream connects, disconnects, or errors.
    """
    form_data = await request.form()
    stream_status = form_data.get("StreamStatus", "")
    stream_sid = form_data.get("StreamSid", "")
    call_sid = form_data.get("CallSid", "")

    logger.info(
        "twilio_stream_status: {status} stream={stream_sid}",
        status=stream_status,
        stream_sid=stream_sid,
        call_sid=call_sid,
    )

    return Response(status_code=204)


# --- Call transfer callbacks (ADR-0009) ---


def _say_twiml(text: str, *, hangup: bool = False) -> str:
    """Build a <Response><Say>…</Say></Response> (ElementTree escapes the text)."""
    response = ET.Element("Response")
    say = ET.SubElement(response, "Say")
    say.text = text
    if hangup:
        ET.SubElement(response, "Hangup")
    return ET.tostring(response, encoding="unicode")


def _normalize_answered_by(answered_by: str) -> str:
    """Collapse Twilio's AnsweredBy values to human / machine / fax / unknown."""
    if answered_by == "human":
        return "human"
    if answered_by.startswith("machine"):
        return "machine"
    if answered_by == "fax":
        return "fax"
    return "unknown"


@router.post("/transfer-whisper/{call_id}")
async def transfer_whisper(
    request: Request,
    session: DbSession,
    call_id: UUID,
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Briefing played to the operator on a warm transfer, before bridging."""
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    from turncall.services import transfer as transfer_svc

    intent = await transfer_svc.load_transfer_intent(call_id)
    text = (
        await transfer_svc.resolve_briefing_text(session, call_id, intent)
        if intent
        else None
    )
    # No briefing (or intent expired) → bridge with no whisper rather than error.
    twiml = _say_twiml(text) if text else "<Response></Response>"
    return Response(content=twiml, media_type=TWIML_CONTENT_TYPE)


@router.post("/transfer-result/{call_id}")
async def transfer_result(
    request: Request,
    session: DbSession,
    call_id: UUID,
    dial_call_status: str = Form(default="", alias="DialCallStatus"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """Dial-result callback — graceful fallback when the operator doesn't answer."""
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    # "completed" means the legs bridged and then ended — nothing more to say.
    if dial_call_status == "completed":
        return Response(content="<Response></Response>", media_type=TWIML_CONTENT_TYPE)

    from turncall.services import transfer as transfer_svc

    intent = await transfer_svc.load_transfer_intent(call_id)
    message = (
        intent.fallback_message if intent and intent.fallback_message else None
    ) or "Sorry, we couldn't connect you right now. Goodbye."
    logger.info(
        "transfer_no_answer", call_id=str(call_id), dial_status=dial_call_status
    )
    return Response(
        content=_say_twiml(message, hangup=True), media_type=TWIML_CONTENT_TYPE
    )


@router.post("/transfer-amd/{call_id}")
async def transfer_amd(
    request: Request,
    session: DbSession,
    call_id: UUID,
    answered_by: str = Form(default="", alias="AnsweredBy"),
    x_twilio_signature: str = Header(default="", alias="X-Twilio-Signature"),
) -> Response:
    """AMD notify — record whether the operator leg was a human or voicemail."""
    form_data = await request.form()
    if not _validate_twilio_request(request, x_twilio_signature, dict(form_data)):
        return Response(status_code=403)

    call = await call_repo.get_call_by_id(session, call_id)
    if call is None:
        return Response(status_code=204)

    from turncall.events.emit import emit_call_event
    from turncall.services import transfer as transfer_svc

    intent = await transfer_svc.load_transfer_intent(call_id)
    await emit_call_event(
        session,
        call_id=call_id,
        project_id=call.project_id,
        event_type=CallEventType.TRANSFER_ANSWERED,
        payload={
            "target_number": intent.target_number if intent else None,
            "answered_by": _normalize_answered_by(answered_by),
        },
    )
    return Response(status_code=204)
