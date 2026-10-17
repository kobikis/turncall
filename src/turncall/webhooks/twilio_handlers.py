"""Twilio webhook handlers for inbound calls, status callbacks, and recordings."""

import xml.etree.ElementTree as ET
import xml.sax.saxutils
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.api.errors import RateLimitedError
from turncall.config import get_settings
from turncall.domain.call_state import (
    TWILIO_STATUS_MAP,
    is_terminal_state,
    is_valid_transition,
)
from turncall.domain.carriers import TWILIO
from turncall.domain.enums import CallEventType, CallStatus
from turncall.services import telemetry_redaction
from turncall.services.limits import release_live_call
from turncall.services.media_token import media_token
from turncall.storage.models import CallRow
from turncall.storage.repositories import (
    agent_repo,
    call_repo,
    phone_number_repo,
)
from turncall.webhooks.twilio_signature import validate_twilio_signature

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio-webhooks"])

TWIML_CONTENT_TYPE = "application/xml"
# Over the live-call limit (#224): refused before answering, so nothing is
# billed for saying no, and the caller hears busy and can try again.
BUSY_TWIML = '<Response><Reject reason="busy"/></Response>'


def _public_origin(request: Request) -> tuple[str, str]:
    """The scheme and host Twilio reaches this deployment at.

    The one source of both the callback URLs this edge hands Twilio (TwiML's
    `statusCallback`, the media stream) and the URL a signature is checked
    against, so the two cannot disagree. Behind a tunnel or proxy (ngrok, a
    load balancer) the container sees plain http and an internal host, so
    X-Forwarded-Proto/Host are honoured (a proxy chain is comma-separated:
    the first is the client's). Production is always https, as Twilio is told
    it is, even behind a TLS terminator that sends no X-Forwarded-Proto.
    """
    fwd_proto = request.headers.get("x-forwarded-proto")
    if get_settings().is_production:
        scheme = "https"
    elif fwd_proto:
        scheme = fwd_proto.split(",")[0].strip()
    else:
        scheme = request.url.scheme
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host")
    host = host_header.split(",")[0].strip() if host_header else request.url.netloc
    return scheme, host


def _public_url(request: Request) -> str:
    """The externally-visible URL of this request, as Twilio signed it."""
    scheme, host = _public_origin(request)
    url = f"{scheme}://{host}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


async def _validate_twilio_request(
    request: Request,
    session: AsyncSession,
    signature: str,
    params: dict[str, str],
) -> bool:
    """Validate a Twilio signature with the token of the account that sent it.

    A number bound with its own Twilio account is signed by that account's
    token, matched by the `AccountSid` Twilio puts in every webhook; anything
    else is checked against the platform's (services/twilio_accounts.py).
    """
    from turncall.services.twilio_accounts import (
        token_for_account_sid,
        unverifiable_webhooks_refused,
    )
    from turncall.services.webhook_verification import refused_webhooks

    settings = get_settings()
    token = await token_for_account_sid(session, params.get("AccountSid"), settings)
    if not token:
        # No platform token and no number on this account. In development with
        # no Twilio credentials anywhere this passes, as before. In production
        # (#337), or once numbers bring their own accounts, it is refused: a
        # blank token must not turn the check off.
        if not await unverifiable_webhooks_refused(session, settings):
            return True
        # Anyone can cause this, as often as they like, so it is said once a
        # minute per route; startup has already reported the configuration.
        refused_webhooks.warn(
            getattr(request.scope.get("route"), "path", "/webhooks/twilio"),
            "twilio_webhook_unverifiable: no Twilio auth token can verify this "
            "webhook (TWILIO_AUTH_TOKEN unset, and no number has account "
            "{account_sid}); refused",
            account_sid=params.get("AccountSid", ""),
        )
        return False
    url = _public_url(request)
    return validate_twilio_signature(token, signature, url, params)


def generate_media_stream_twiml(request: Request, call_id: UUID) -> str:
    """TwiML answering a call with a bidirectional media stream (ADR-0022).

    Twilio's answer format, so it lives at Twilio's edge rather than on the
    carrier adapter. The call id and its media token are passed — the token
    is what proves the stream that opens is this call's — and the media entry
    point resolves everything else from the call record. wss:// when behind
    HTTPS (ngrok, production): see `_public_origin`.
    """
    scheme, host = _public_origin(request)
    ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{host}/ws/media-stream"
    # Signed by Twilio over exactly this URL, and checked against
    # `_public_url` of the callback, which rebuilds it from the same origin.
    status_cb = f"{scheme}://{host}{router.prefix}/stream-status"
    return (
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" statusCallback="{status_cb}">'
        f'<Parameter name="callId" value="{call_id}" />'
        f'<Parameter name="mediaToken" value="{media_token(call_id)}" />'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )


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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
        logger.warning("twilio_sms_invalid_signature")
        return Response(status_code=403)

    # Normalize phone numbers (Twilio may URL-encode '+' as space)
    to_number = to_number.strip()
    from_number = from_number.strip()
    if not to_number.startswith("+"):
        to_number = f"+{to_number}"
    if not from_number.startswith("+"):
        from_number = f"+{from_number}"

    # Verify the phone number exists and has SMS enabled. Its project known,
    # this request's logs follow the project's redaction (#314).
    phone_number = await phone_number_repo.get_by_e164(session, to_number)
    if phone_number is not None:
        await telemetry_redaction.for_project(phone_number.project_id, session=session)
    logger.info(
        "twilio_inbound_sms",
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body_length=len(body),
    )
    if phone_number is None:
        logger.warning("twilio_sms_unknown_number", to_number=to_number)
        return _sms_twiml("This number is not configured for SMS.")

    if not phone_number.sms_enabled:
        logger.warning("twilio_sms_not_enabled", to_number=to_number)
        return _sms_twiml("SMS is not enabled on this number.")

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
        return _sms_twiml(result.reply_text)
    except RateLimitedError as exc:
        # Over the session's messages per minute (#226): no LLM, no reply.
        logger.warning(
            "twilio_sms_rate_limited", from_number=from_number, **exc.details
        )
        return _sms_twiml(None)
    except ValueError as exc:
        logger.error("twilio_sms_error", error=str(exc))
        return _sms_twiml("Sorry, I'm unable to process your message right now.")
    except Exception:
        logger.exception("twilio_sms_unexpected_error")
        return _sms_twiml("An error occurred. Please try again later.")


def _sms_twiml(text: str | None) -> Response:
    """TwiML answering an SMS with `text` (escaped), or with nothing at all."""
    if text is None:
        return Response(content="<Response/>", media_type=TWIML_CONTENT_TYPE)
    message = xml.sax.saxutils.escape(text)
    return Response(
        content=f"<Response><Message>{message}</Message></Response>",
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
        logger.warning("twilio_invalid_signature")
        return Response(status_code=403)

    # Resolve phone number to routing target. Its project known, this
    # request's logs follow the project's redaction (#314).
    phone_number = await phone_number_repo.get_by_e164(session, to_number)
    if phone_number is not None:
        await telemetry_redaction.for_project(phone_number.project_id, session=session)
    logger.info(
        "twilio_inbound_call",
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        status=call_status,
    )
    if phone_number is None or phone_number.provider != TWILIO:
        logger.warning("twilio_inbound_unknown_number", to_number=to_number)
        return Response(
            content="<Response><Say>This number is not configured.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    from turncall.services.inbound_call import InboundRefusal, open_inbound_call

    call = await open_inbound_call(
        session,
        phone_number,
        carrier=TWILIO,
        from_number=from_number,
        to_number=to_number,
        provider_call_sid=call_sid,
    )
    if isinstance(call, InboundRefusal):
        # Busy is refused unanswered; any other refusal is answered and told why.
        return Response(
            content=BUSY_TWIML if call.busy else _say_twiml(call.message, hangup=True),
            media_type=TWIML_CONTENT_TYPE,
        )

    twiml = generate_media_stream_twiml(request, call.id)
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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

    # Load agent from the call record — or call-init's inline one, which has
    # no row (#277, ADR-0017).
    agent = None
    if call.active_agent_id:
        agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)

    if agent is None and not call.metadata_json.get("dynamic_config"):
        return Response(
            content="<Response><Say>Agent not available.</Say><Hangup/></Response>",
            media_type=TWIML_CONTENT_TYPE,
        )

    twiml = generate_media_stream_twiml(request, call.id)

    logger.info(
        "twilio_outbound_connected",
        call_id=str(call.id),
        agent_id=str(agent.id) if agent else "dynamic",
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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
    # The call's ending (post-call analysis, its webhooks) logs from here.
    await telemetry_redaction.for_project(call.project_id, session=session)

    # Map Twilio status to internal status
    new_status = TWILIO_STATUS_MAP.get(call_status)
    if new_status is None:
        logger.warning(
            "twilio_status_unmapped", call_sid=call_sid, twilio_status=call_status
        )
        return Response(status_code=204)

    if is_terminal_state(new_status):
        # The carrier says the call is over, whatever our state machine thinks
        # — often the only ending an unanswered call gets (#224).
        await release_live_call(call.project_id, call.id)

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

    await _record_status(session, call, new_status, call_sid, call_status)

    # Trigger post-call processing when call completes
    if new_status == CallStatus.COMPLETED:
        try:
            from turncall.services.call_analysis_trigger import (
                config_for_call,
                trigger_post_call_analysis,
            )
            from turncall.storage.database import get_session_factory

            # Not gated on active_agent_id: a call running an inline agent has
            # none, and this is what dispatches call.ended.
            config = await config_for_call(session, call)
            if config is not None:
                trigger_post_call_analysis(
                    get_session_factory(),
                    call.id,
                    call.project_id,
                    config,
                )
        except Exception:
            logger.exception("analysis_trigger_error", call_id=str(call.id))

    return Response(status_code=204)


async def _record_status(
    session: AsyncSession,
    call: CallRow,
    new_status: CallStatus,
    call_sid: str,
    twilio_status: str,
) -> None:
    """The call event a status callback leaves, as Telnyx's edge leaves one.

    - answered (`in-progress`): `call.started`, stored and sent.
    - `failed`/`canceled`: `call.failed`, stored and sent, which reads back
      as `telephony_failed`.
    - `completed`, `busy`, `no-answer`: the carrier's `call.ended` row,
      stored only. A completed call's subscribers hear the comprehensive
      `call.ended` from post-call processing (ADR-0006); an unanswered one's
      status, and so its `ended_reason`, is on the call row.
    - `ringing`, `queued`: nothing beyond the call row's status. A call that
      rings out is not a call that started.
    """
    from turncall.events.emit import emit_call_event, record_call_event

    payload = {
        "provider_call_sid": call_sid,
        "twilio_status": twilio_status,
        "internal_status": new_status.value,
        "from_number": call.from_number,
        "to_number": call.to_number,
    }
    sent = {
        CallStatus.IN_PROGRESS: CallEventType.CALL_STARTED,
        CallStatus.FAILED: CallEventType.CALL_FAILED,
    }.get(new_status)
    if sent is not None:
        await emit_call_event(
            session,
            call_id=call.id,
            project_id=call.project_id,
            event_type=sent,
            payload=payload,
        )
    elif new_status in {CallStatus.COMPLETED, CallStatus.BUSY, CallStatus.NO_ANSWER}:
        await record_call_event(
            session,
            call_id=call.id,
            project_id=call.project_id,
            event_type=CallEventType.CALL_ENDED,
            payload=payload,
        )


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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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
            from turncall.services.twilio_accounts import account_for_call

            account = await account_for_call(session, call, get_settings())
            stored_url = await download_and_store_recording(
                recording_url,
                call.id,
                recording_sid,
                auth=(account.account_sid, account.auth_token) if account else None,
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
    session: DbSession,
) -> Response:
    """Handle Twilio Media Stream status callback.

    Notifies when the audio stream connects, disconnects, or errors.
    """
    form_data = await request.form()
    # Verified like every Twilio webhook (#337). The header is read here rather
    # than declared, so the published API spec is unchanged.
    if not await _validate_twilio_request(
        request,
        session,
        request.headers.get("X-Twilio-Signature", ""),
        dict(form_data),
    ):
        return Response(status_code=403)
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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
    if not await _validate_twilio_request(
        request, session, x_twilio_signature, dict(form_data)
    ):
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
