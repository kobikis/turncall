"""WhatsApp webhook handlers for voice calls and text messages.

Handles:
  - GET  /webhooks/whatsapp  — Webhook verification (hub.mode challenge)
  - POST /webhooks/whatsapp  — Voice call events (connect/terminate) and text messages
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.api.errors import RateLimitedError
from turncall.config import Settings, get_settings
from turncall.domain.enums import CallDirection, CallEventType
from turncall.domain.models import AgentConfig
from turncall.orchestrator.ambience import build_ambience_mixer
from turncall.orchestrator.pipeline_builder import start_call_pipeline
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
from turncall.orchestrator.transport_factory import (
    WEBRTC_SAMPLE_RATE,
    create_whatsapp_transport,
)
from turncall.services import telemetry_redaction
from turncall.services.limits import admit_inbound_call, release_live_call
from turncall.storage.database import create_session_factory, get_engine
from turncall.storage.repositories import (
    agent_repo,
    call_repo,
    phone_number_repo,
)
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


async def warn_if_unverifiable(settings: Settings, session: AsyncSession) -> str | None:
    """At startup, warn when WhatsApp is configured but WHATSAPP_APP_SECRET is not (#339).

    WhatsApp counts as configured when any other WHATSAPP_* setting is set
    (the access token, phone number id or webhook verify token) or a phone
    number is bound with `whatsapp_enabled`. Returns the warning, or None.
    """
    whatsapp = settings.whatsapp
    if whatsapp.app_secret:
        return None
    configured = [
        name
        for name, value in (
            ("WHATSAPP_TOKEN", whatsapp.token),
            ("WHATSAPP_PHONE_NUMBER_ID", whatsapp.phone_number_id),
            ("WHATSAPP_WEBHOOK_VERIFY_TOKEN", whatsapp.webhook_verify_token),
        )
        if value
    ]
    if await phone_number_repo.any_whatsapp_enabled(session):
        configured.append("a whatsapp_enabled phone number")
    if not configured:
        return None
    consequence = (
        "every WhatsApp webhook is refused (403)"
        if settings.is_production
        else "WhatsApp webhooks are accepted unverified"
    )
    message = (
        f"whatsapp_app_secret_missing | WhatsApp is configured "
        f"({', '.join(configured)}) but WHATSAPP_APP_SECRET is not set: "
        f"{consequence}"
    )
    logger.warning(message)
    return message


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
    # Without WHATSAPP_APP_SECRET nothing can verify one: production refuses it
    # before anything is read (#339); development accepts it unverified.
    settings = get_settings()
    if not settings.whatsapp.app_secret and settings.is_production:
        logger.warning(
            "whatsapp_webhook_refused | WHATSAPP_APP_SECRET is not set, so its "
            "X-Hub-Signature-256 cannot be verified"
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
    from pipecat.transports.whatsapp.api import WhatsAppApi, WhatsAppWebhookRequest
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

    # A connect over the project's live-call limit (#224) is rejected before
    # it is accepted, so the caller is never answered only to be refused.
    call_id = uuid4()
    from_number, to_number, whatsapp_call_id = _call_parties(body)
    phone_row = (
        await phone_number_repo.get_by_e164(session, to_number)
        if _is_connect(body)
        else None
    )
    if phone_row is not None:
        # This call's logs follow its project's redaction (#314).
        await telemetry_redaction.for_project(phone_row.project_id, session=session)
    if phone_row is not None and not await admit_inbound_call(
        session,
        project_id=phone_row.project_id,
        call_id=call_id,
        transport="whatsapp",
        from_number=from_number,
        to_number=to_number,
        provider_call_id=whatsapp_call_id,
    ):
        api = WhatsAppApi(
            settings.whatsapp.token, settings.whatsapp.phone_number_id, http_session
        )
        try:
            await api.reject_call_to_whatsapp(whatsapp_call_id)
        except Exception:
            logger.exception("whatsapp_call_reject_error")
        return JSONResponse({"status": "rejected"})

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        """Callback invoked when a WhatsApp call is connected via WebRTC."""
        await _start_voice_pipeline(connection, body, call_id)

    try:
        await whatsapp_client.handle_webhook_request(
            request=webhook_request,
            connection_callback=on_connection,
        )
    except Exception:
        logger.exception("whatsapp_call_event_error")
        if phone_row is not None:
            await release_live_call(phone_row.project_id, call_id)
        return JSONResponse({"status": "error"}, status_code=500)

    return JSONResponse({"status": "ok"})


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


async def _start_voice_pipeline(
    connection: object,
    body: dict[str, Any],
    call_id: UUID,
) -> None:
    """Build and start a Pipecat voice pipeline for a WhatsApp call."""
    settings = get_settings()
    engine = get_engine()
    session_factory = create_session_factory(engine)
    from_number, to_number, whatsapp_call_id = _call_parties(body)

    # Resolve phone number -> agent
    async with session_factory() as db:
        phone_row = await phone_number_repo.get_by_e164(db, to_number)
        if phone_row is None:
            logger.warning(
                "whatsapp_call_unknown_number",
                to_number=to_number,
                from_number=from_number,
                whatsapp_call_id=whatsapp_call_id,
            )
            return
        await telemetry_redaction.for_project(phone_row.project_id, session=db)

        agent = None
        dynamic_config = None
        template_variables: dict[str, str] = {}
        request_metadata: dict = {}
        knowledge_context: str | None = None

        if phone_row.routing_target_type == "webhook" and phone_row.server_url:
            # Dynamic agent resolution via server event
            from turncall.events.server_events import send_call_init
            from turncall.services.call_init_resolver import (
                resolve_call_init,
            )

            response = await send_call_init(
                phone_row.server_url,
                call_id=str(call_id),
                from_number=from_number,
                to_number=to_number,
                transport_type="inboundWhatsAppCall",
                provider_call_id=whatsapp_call_id,
                secret=getattr(phone_row, "server_url_secret", None),
            )
            if response.success and response.data:
                result = await resolve_call_init(db, response.data)
                agent = result.agent
                dynamic_config = result.dynamic_config
                template_variables = result.template_variables
                request_metadata = result.metadata
                knowledge_context = result.knowledge_context

        elif phone_row.routing_target_type == "agent" and phone_row.routing_target_id:
            agent = await agent_repo.get_agent_by_id(db, phone_row.routing_target_id)

        if agent is None and dynamic_config is None:
            logger.warning(
                "whatsapp_call_no_agent",
                to_number=to_number,
                from_number=from_number,
                whatsapp_call_id=whatsapp_call_id,
            )
            await release_live_call(phone_row.project_id, call_id)
            return

        # Build config from agent or inline dynamic config
        if dynamic_config:
            config = AgentConfig.model_validate(dynamic_config)
        else:
            config = AgentConfig.model_validate(agent.config_blob)

        # Apply template variables
        if template_variables:
            from turncall.services.template_renderer import render_agent_config

            config = render_agent_config(config, template_variables)

        # Apply knowledge context
        if knowledge_context:
            from turncall.services.template_renderer import prepend_knowledge_context

            config = prepend_knowledge_context(config, knowledge_context)

        # Create call record
        agent_id_for_call = agent.id if agent else None
        call_metadata: dict = {
            "transport": "whatsapp",
            "whatsapp_call_id": whatsapp_call_id,
            **({"dynamic_config": dynamic_config} if dynamic_config else {}),
            **(
                {"template_variables": template_variables} if template_variables else {}
            ),
            **({"metadata": request_metadata} if request_metadata else {}),
            **({"knowledge_context": knowledge_context} if knowledge_context else {}),
        }
        call = await call_repo.create_call(
            db,
            project_id=phone_row.project_id,
            call_id=call_id,
            direction=CallDirection.INBOUND,
            from_number=from_number,
            to_number=to_number,
            active_agent_id=agent_id_for_call,
            metadata_json=call_metadata,
        )

        # Recorded and dispatched, as every carrier's inbound call is — these
        # were stored only, so no subscriber heard a WhatsApp call start.
        from turncall.events.emit import emit_call_event

        await emit_call_event(
            db,
            call_id=call.id,
            project_id=phone_row.project_id,
            event_type=CallEventType.CALL_INITIALIZING,
            payload={
                "from_number": from_number,
                "to_number": to_number,
                "direction": "inbound",
                "transport": "whatsapp",
            },
        )
        await emit_call_event(
            db,
            call_id=call.id,
            project_id=phone_row.project_id,
            event_type=CallEventType.CALL_STARTED,
            payload={
                "whatsapp_call_id": whatsapp_call_id,
                "from_number": from_number,
                "to_number": to_number,
                "agent_id": (str(agent_id_for_call) if agent_id_for_call else None),
                "agent_name": agent.name if agent else "dynamic",
            },
        )
        await db.commit()

    call_context = CallContext(
        call_id=call.id,
        project_id=phone_row.project_id,
        agent_id=agent_id_for_call or DYNAMIC_AGENT_ID,
        call_sid=f"whatsapp:{whatsapp_call_id}",
        stream_sid=f"whatsapp:{whatsapp_call_id}",
        session_factory=session_factory,
    )

    # Build transport and pipeline
    mixer = await build_ambience_mixer(
        config,
        project_id=call_context.project_id,
        sample_rate=WEBRTC_SAMPLE_RATE,
        session_factory=session_factory,
        settings=settings,
    )
    transport = create_whatsapp_transport(connection, mixer=mixer)

    await start_call_pipeline(
        config=config,
        transport=transport,
        call_context=call_context,
        settings=settings,
        session_factory=session_factory,
        audio_sample_rate=WEBRTC_SAMPLE_RATE,
    )

    logger.info(
        "whatsapp_voice_call_started",
        call_id=str(call.id),
        agent_id=str(agent_id_for_call) if agent_id_for_call else "dynamic",
        whatsapp_call_id=whatsapp_call_id,
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
