"""WhatsApp webhook handlers for voice calls and text messages.

Handles:
  - GET  /webhooks/whatsapp  — Webhook verification (hub.mode challenge)
  - POST /webhooks/whatsapp  — Voice call events (connect/terminate) and text messages
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.config import get_settings
from turncall.domain.enums import CallDirection, CallEventType
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_builder import build_call_pipeline
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.transport_factory import create_whatsapp_transport
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

    # Validate signature for ALL webhook events (CRITICAL security check)
    settings = get_settings()
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
                return await _handle_call_event(body, request)

            if field == "messages":
                return await _handle_message_event(change, session)

    logger.debug("whatsapp_webhook_no_handler", body_keys=list(body.keys()))
    return JSONResponse({"status": "ok"})


async def _handle_call_event(
    body: dict[str, Any],
    request: Request,
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

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        """Callback invoked when a WhatsApp call is connected via WebRTC."""
        await _start_voice_pipeline(connection, body)

    try:
        await whatsapp_client.handle_webhook_request(
            request=webhook_request,
            connection_callback=on_connection,
        )
    except Exception:
        logger.exception("whatsapp_call_event_error")
        return JSONResponse({"status": "error"}, status_code=500)

    return JSONResponse({"status": "ok"})


async def _start_voice_pipeline(
    connection: object,
    body: dict[str, Any],
) -> None:
    """Build and start a Pipecat voice pipeline for a WhatsApp call."""
    settings = get_settings()
    engine = get_engine()
    session_factory = create_session_factory(engine)

    # Extract caller info from webhook body
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

    # Normalize phone numbers
    to_number = _normalize_phone(to_number)
    from_number = _normalize_phone(from_number)

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

        agent = None
        dynamic_config = None
        template_variables: dict[str, str] = {}
        request_metadata: dict = {}
        knowledge_context: str | None = None

        if phone_row.routing_target_type == "webhook" and phone_row.server_url:
            # Dynamic agent resolution via server event
            from uuid import uuid4

            from turncall.events.server_events import send_call_init
            from turncall.services.call_init_resolver import (
                resolve_call_init,
            )

            temp_call_id = str(uuid4())
            response = await send_call_init(
                phone_row.server_url,
                call_id=temp_call_id,
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
            direction=CallDirection.INBOUND,
            from_number=from_number,
            to_number=to_number,
            active_agent_id=agent_id_for_call,
            metadata_json=call_metadata,
        )

        # Fire call.initializing event
        init_seq = await call_repo.get_next_sequence_number(db, call.id)
        await call_repo.create_call_event(
            db,
            call_id=call.id,
            event_type=CallEventType.CALL_INITIALIZING,
            payload={
                "from_number": from_number,
                "to_number": to_number,
                "direction": "inbound",
                "transport": "whatsapp",
            },
            sequence_number=init_seq,
        )

        seq = await call_repo.get_next_sequence_number(db, call.id)
        await call_repo.create_call_event(
            db,
            call_id=call.id,
            event_type=CallEventType.CALL_STARTED,
            payload={
                "whatsapp_call_id": whatsapp_call_id,
                "from_number": from_number,
                "to_number": to_number,
                "agent_id": (str(agent_id_for_call) if agent_id_for_call else None),
                "agent_name": agent.name if agent else "dynamic",
            },
            sequence_number=seq,
        )
        await db.commit()

    from uuid import UUID as _UUID

    call_context = CallContext(
        call_id=call.id,
        project_id=phone_row.project_id,
        agent_id=agent_id_for_call or _UUID(int=0),
        call_sid=f"whatsapp:{whatsapp_call_id}",
        stream_sid=f"whatsapp:{whatsapp_call_id}",
        session_factory=session_factory,
    )

    # Build transport and pipeline
    transport = create_whatsapp_transport(connection)

    call_session = await build_call_pipeline(
        config=config,
        transport=transport,
        call_context=call_context,
        settings=settings,
        session_factory=session_factory,
        audio_sample_rate=16000,
    )
    asyncio.create_task(call_session.start())  # noqa: RUF006

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

        # Normalize phone numbers
        to_number = _normalize_phone(display_phone)
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
