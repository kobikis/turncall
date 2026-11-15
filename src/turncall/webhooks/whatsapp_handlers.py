"""WhatsApp webhook handlers for voice calls and text messages.

Handles:
  - GET  /webhooks/whatsapp  — Webhook verification (hub.mode challenge)
  - POST /webhooks/whatsapp  — Voice call events (connect/terminate) and text messages
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.api.errors import RateLimitedError
from turncall.config import get_settings
from turncall.domain.carriers import DEFAULT_CARRIER
from turncall.orchestrator.call_runner import CallToRun, TransportFactory, run_call
from turncall.orchestrator.transport_factory import (
    WEBRTC_SAMPLE_RATE,
    create_whatsapp_transport,
)
from turncall.services import telemetry_redaction
from turncall.services.call_opening import (
    INBOUND_WHATSAPP_CALL,
    CallParties,
    CallRefusal,
    EdgeHandles,
    NumberSource,
    OpenedCall,
    open_call,
)
from turncall.services.limits import release_live_call
from turncall.services.webhook_verification import (
    refused_webhooks,
    whatsapp_unverifiable_refused,
)
from turncall.storage.database import create_session_factory, get_engine
from turncall.storage.repositories import phone_number_repo
from turncall.storage.sealing import CredentialKeyError
from turncall.webhooks.whatsapp_signature import validate_whatsapp_signature

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp-webhooks"])

# Module-level aiohttp session with lock for thread safety
_http_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _get_http_session() -> aiohttp.ClientSession:
    """Get or create the module-level aiohttp session (thread-safe)."""
    global _http_session
    async with _session_lock:
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession()
    return _http_session


async def close_http_session() -> None:
    """Close the module-level aiohttp session (call on shutdown)."""
    global _http_session
    async with _session_lock:
        if _http_session is not None and not _http_session.closed:
            await _http_session.close()
            _http_session = None


@router.get("")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Handle WhatsApp webhook verification (GET).

    WhatsApp sends hub.mode, hub.challenge, and hub.verify_token
    as query parameters. We must return the challenge value.
    """
    settings = get_settings()
    params = dict(request.query_params)

    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")

    if mode != "subscribe":
        logger.warning("whatsapp_verify_invalid_mode", mode=mode)
        return PlainTextResponse("Invalid mode", status_code=403)

    if not challenge or not verify_token:
        logger.warning("whatsapp_verify_missing_params")
        return PlainTextResponse("Missing parameters", status_code=400)

    expected = settings.whatsapp.webhook_verify_token
    if verify_token != expected:
        logger.warning("whatsapp_verify_token_mismatch")
        return PlainTextResponse("Token mismatch", status_code=403)

    logger.info("whatsapp_webhook_verified")
    return PlainTextResponse(challenge)


@router.post("")
async def handle_webhook(
    request: Request,
    session: DbSession,
) -> Response:
    """Handle WhatsApp webhook events (POST).

    Validates signature for ALL events, then dispatches to voice call
    handler or text message handler based on the `field` in each change.
    """
    raw_body = await request.body()

    # Validate signature for ALL webhook events (CRITICAL security check).
    # Without WHATSAPP_APP_SECRET nothing can verify one: it is refused before
    # anything is read everywhere but development, which accepts it unverified
    # (#339). Said once a minute: anyone can cause it, and startup has already
    # reported the configuration.
    settings = get_settings()
    if not settings.whatsapp.app_secret and whatsapp_unverifiable_refused(settings):
        refused_webhooks.warn(
            "/webhooks/whatsapp",
            "whatsapp_webhook_refused | WHATSAPP_APP_SECRET is not set, so its "
            "X-Hub-Signature-256 cannot be verified",
        )
        return Response(status_code=403)
    if settings.whatsapp.app_secret:
        sha256_signature = request.headers.get("X-Hub-Signature-256", "")
        if not validate_whatsapp_signature(
            settings.whatsapp.app_secret, raw_body, sha256_signature
        ):
            sig_preview = sha256_signature[:30] if sha256_signature else "(empty)"
            logger.warning(
                f"whatsapp_invalid_signature | sig={sig_preview} "
                f"secret_len={len(settings.whatsapp.app_secret)} "
                f"body_len={len(raw_body)}"
            )
            return Response(status_code=403)

    body = await request.json()

    # Dispatch based on event type
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")

            if field == "calls":
                return await _handle_call_event(body, request, session)

            if field == "messages":
                return await _handle_message_event(change, session)

    logger.debug("whatsapp_webhook_no_handler", body_keys=list(body.keys()))
    return JSONResponse({"status": "ok"})


async def _handle_call_event(
    body: dict[str, Any],
    request: Request,
    session: AsyncSession,
) -> Response:
    """Handle WhatsApp voice call events using Pipecat WhatsAppClient.

    Note: signature is already validated at the top level. We pass
    whatsapp_secret=None to avoid double validation inside Pipecat.
    """
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest
    from pipecat.transports.whatsapp.client import WhatsAppClient

    settings = get_settings()
    http_session = await _get_http_session()

    # No whatsapp_secret — signature already validated at top level
    whatsapp_client = WhatsAppClient(
        whatsapp_token=settings.whatsapp.token,
        phone_number_id=settings.whatsapp.phone_number_id,
        session=http_session,
        whatsapp_secret=None,
    )

    webhook_request = WhatsAppWebhookRequest.model_validate(body)

    # A connect is opened before pipecat's client pre-accepts and accepts it:
    # one over the project's live-call limit (#224), or with no agent that can
    # run (#483), is rejected, so the caller is never answered only to be
    # refused or left in silence.
    call: OpenedCall | None = None
    if _is_connect(body):
        call = await _open_connect(session, body)
        if call is None:
            await _reject(http_session, _call_parties(body)[2])
            return JSONResponse({"status": "rejected"})

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        """Callback invoked when a WhatsApp call is connected via WebRTC."""
        if call is None:
            logger.warning("whatsapp_connection_without_a_connect")
            return
        await _start_voice_pipeline(connection, call)

    try:
        await whatsapp_client.handle_webhook_request(
            request=webhook_request,
            connection_callback=on_connection,
        )
    except Exception:
        logger.exception("whatsapp_call_event_error")
        if call is not None:
            await _fail_unanswered(call)
        return JSONResponse({"status": "error"}, status_code=500)

    return JSONResponse({"status": "ok"})


async def _open_connect(
    session: AsyncSession, body: dict[str, Any]
) -> OpenedCall | None:
    """The connect's call, opened (`open_call`: the slot, the agent, the row
    and its start events), or None to reject it: a number nobody bound, a
    project at its live-call limit (announced as `call.rejected`), or no
    agent that can run. Nothing of a refused one is left, slot included."""
    from_number, to_number, whatsapp_call_id = _call_parties(body)
    phone_row = await phone_number_repo.get_by_e164(session, to_number)
    if phone_row is None:
        logger.warning(
            "whatsapp_call_unknown_number",
            to_number=to_number,
            from_number=from_number,
            whatsapp_call_id=whatsapp_call_id,
        )
        return None
    source = NumberSource(
        number=phone_row,
        parties=CallParties(from_number=from_number, to_number=to_number),
        handles=EdgeHandles(
            carrier=DEFAULT_CARRIER,
            transport="whatsapp",
            transport_call_id=whatsapp_call_id,
        ),
        call_type=INBOUND_WHATSAPP_CALL,
        # A/B weights are a phone call's: a WhatsApp call runs call-init's
        # agent, else the bound one.
        weighted=False,
    )
    try:
        opened = await open_call(
            source, project_id=phone_row.project_id, session=session
        )
    except Exception:
        # The slot is given back already: the caller is refused all the same.
        logger.exception("whatsapp_call_agent_error", whatsapp_call_id=whatsapp_call_id)
        await session.rollback()
        return None
    if isinstance(opened, CallRefusal):
        if not opened.busy:
            logger.warning(
                "whatsapp_call_no_agent",
                to_number=to_number,
                from_number=from_number,
                whatsapp_call_id=whatsapp_call_id,
            )
        return None
    return opened


async def _fail_unanswered(call: OpenedCall) -> None:
    """An opened call pipecat's client could not answer: no pipeline will
    ever hold its slot or end it, so both happen here."""
    from turncall.orchestrator.call_session import finalize_call

    await release_live_call(call.project_id, call.call_id)
    await finalize_call(create_session_factory(get_engine()), call.call_id, failed=True)


async def _reject(http_session: aiohttp.ClientSession, whatsapp_call_id: str) -> None:
    """Refuse a connect at WhatsApp, unanswered. Best effort: a refusal that
    fails is logged."""
    from pipecat.transports.whatsapp.api import WhatsAppApi

    settings = get_settings()
    api = WhatsAppApi(
        settings.whatsapp.token, settings.whatsapp.phone_number_id, http_session
    )
    try:
        await api.reject_call_to_whatsapp(whatsapp_call_id)
    except Exception:
        logger.exception("whatsapp_call_reject_error")


def _call_parties(body: dict[str, Any]) -> tuple[str, str, str]:
    """(from, to, WhatsApp call id) of a call event, numbers in E.164."""
    from_number = ""
    to_number = ""
    whatsapp_call_id = ""
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            calls = value.get("calls", [])
            if calls:
                from_number = calls[0].get("from", "")
                to_number = calls[0].get("to", "")
                whatsapp_call_id = calls[0].get("id", "")
            metadata = value.get("metadata", {})
            if metadata.get("phone_number_id"):
                to_number = metadata.get("display_phone_number", to_number)
    return _normalize_phone(from_number), _normalize_phone(to_number), whatsapp_call_id


def _is_connect(body: dict[str, Any]) -> bool:
    """A call starting — the one event that takes a live-call slot."""
    return any(
        call.get("event") == "connect"
        for entry in body.get("entry", [])
        for change in entry.get("changes", [])
        for call in change.get("value", {}).get("calls", [])
    )


async def _start_voice_pipeline(connection: object, call: OpenedCall) -> None:
    """Build and start a Pipecat voice pipeline for an answered WhatsApp call,
    on the call opened before it was answered."""
    settings = get_settings()
    await run_call(
        CallToRun.of(call, session_factory=create_session_factory(get_engine())),
        TransportFactory(
            make=partial(create_whatsapp_transport, connection),
            sample_rate=WEBRTC_SAMPLE_RATE,
        ),
        own_task=True,
        settings=settings,
    )

    logger.info(
        "whatsapp_voice_call_started",
        call_id=str(call.call_id),
        agent_id=str(call.agent.agent_id or "dynamic"),
        whatsapp_call_id=call.start.handles.transport_call_id,
    )


async def _handle_message_event(
    change: dict[str, Any],
    db: AsyncSession,
) -> Response:
    """Handle WhatsApp inbound text messages."""
    settings = get_settings()
    value = change.get("value", {})
    messages = value.get("messages", [])
    metadata = value.get("metadata", {})
    phone_number_id = metadata.get("phone_number_id", "")
    display_phone = metadata.get("display_phone_number", "")
    to_number = _normalize_phone(display_phone)

    # Every message here is to one business number: their logs, the
    # unanswered ones' included, follow its project's redaction (#314).
    ours = await phone_number_repo.get_by_e164(db, to_number) if messages else None
    if ours is not None:
        await telemetry_redaction.for_project(ours.project_id, session=db)

    for msg in messages:
        msg_type = msg.get("type")
        from_number = msg.get("from", "")
        msg_id = msg.get("id", "")

        if msg_type != "text":
            logger.info(
                "whatsapp_unsupported_message_type",
                msg_type=msg_type,
                from_number=from_number,
            )
            continue

        body = msg.get("text", {}).get("body", "")
        if not body:
            continue

        from_number = _normalize_phone(from_number)
        logger.info(
            "whatsapp_inbound_text",
            from_number=from_number,
            to_number=to_number,
            body_length=len(body),
            msg_id=msg_id,
        )

        try:
            from turncall.services.whatsapp_chat import (
                handle_inbound_whatsapp,
                send_whatsapp_text,
            )

            result = await handle_inbound_whatsapp(
                db,
                from_number=from_number,
                to_number=to_number,
                body=body,
                provider_message_id=msg_id,
            )

            # Send reply via WhatsApp Cloud API
            http_session = await _get_http_session()
            await send_whatsapp_text(
                http_session,
                token=settings.whatsapp.token,
                phone_number_id=phone_number_id or settings.whatsapp.phone_number_id,
                to=from_number.lstrip("+"),
                text=result.reply_text,
            )

            logger.info(
                "whatsapp_reply_sent",
                session_id=str(result.session_id),
                is_new_session=result.is_new_session,
            )

        except RateLimitedError as exc:
            # Over the session's messages per minute (#226): no LLM, no reply.
            logger.warning(
                "whatsapp_message_rate_limited", from_number=from_number, **exc.details
            )
        except CredentialKeyError as exc:
            # An inline agent with a secret and no key to seal it (#338):
            # nothing is kept, and the request still commits, so Meta is
            # answered 200 rather than retrying the message.
            await db.rollback()
            logger.error("whatsapp_message_credential_key_unavailable", error=str(exc))
        except ValueError as exc:
            logger.error(f"whatsapp_message_error: {exc}")
        except Exception:
            logger.exception("whatsapp_message_unexpected_error")

    return JSONResponse({"status": "ok"})


def _normalize_phone(phone: str) -> str:
    """Normalize a phone number to E.164 format."""
    phone = phone.strip()
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"
    return phone
