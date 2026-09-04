"""WebRTC signaling via Pipecat SmallWebRTCTransport.

Pipecat 1.0 uses SmallWebRTCRequestHandler for signaling:
  - POST /connect  — SDP offer/answer exchange
  - PATCH /connect — ICE candidate trickle
"""

import asyncio
from dataclasses import fields
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode, NotFoundError
from turncall.auth import Auth
from turncall.config import get_settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_builder import build_call_pipeline
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.transport_factory import (
    STUN_SERVERS,
    create_whatsapp_transport,
)
from turncall.storage.database import create_session_factory, get_engine
from turncall.storage.repositories import agent_repo

router = APIRouter(prefix="/webrtc", tags=["webrtc"])

# SmallWebRTCRequest.from_dict is a bare cls(**data): extra top-level keys like
# agent_id (honored via body.get() in the handler) must be stripped before it.
# Derived from the dataclass so a Pipecat upgrade can't silently drop a field;
# requestData is the camelCase alias from_dict accepts.
_SIGNALING_KEYS = {f.name for f in fields(SmallWebRTCRequest)} | {"requestData"}

# Module-level request handler — manages peer connections across requests
_request_handler = SmallWebRTCRequestHandler(
    ice_servers=STUN_SERVERS,
    connection_mode=ConnectionMode.MULTIPLE,
)


async def _finalize_failed(session_factory: Any, call_id: UUID) -> None:
    """Mark a call FAILED when its pipeline never started (idempotent)."""
    from datetime import UTC, datetime

    from turncall.domain.enums import CallStatus
    from turncall.storage.repositories import call_repo

    try:
        async with session_factory() as db:
            call = await call_repo.get_call_by_id(db, call_id)
            if not call or call.status in ("completed", "failed"):
                return
            ended_at = datetime.now(UTC)
            duration = (
                int((ended_at - call.started_at).total_seconds() * 1000)
                if call.started_at
                else None
            )
            await call_repo.update_call_status(
                db,
                call_id=call_id,
                status=CallStatus.FAILED.value,
                ended_at=ended_at,
                duration_ms=duration,
            )
            await db.commit()
    except Exception:
        logger.exception("failed to finalize orphaned call", call_id=str(call_id))


@router.post("/connect", status_code=200)
async def webrtc_connect(
    request: Request,
    auth: Auth,
    session: DbSession,
) -> JSONResponse:
    """WebRTC signaling endpoint — SDP offer/answer exchange.

    Accepts SDP offer, creates pipeline, returns SDP answer.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request body must be valid JSON",
        ) from exc
    if not isinstance(body, dict):
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request body must be a JSON object",
        )
    logger.info("WebRTC connect request: {keys}", keys=list(body.keys()))

    try:
        webrtc_request = SmallWebRTCRequest.from_dict(
            {k: v for k, v in body.items() if k in _SIGNALING_KEYS}
        )
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message=f"Invalid WebRTC signaling payload: {exc}",
        ) from exc

    # Extract agent_id / server_url from request_data
    request_data = webrtc_request.request_data or {}
    if isinstance(request_data, dict):
        agent_id_str = request_data.get("agent_id") or body.get("agent_id")
        server_url = request_data.get("server_url") or body.get("server_url")
        server_url_secret = request_data.get("server_url_secret") or body.get(
            "server_url_secret"
        )
    else:
        agent_id_str = body.get("agent_id")
        server_url = body.get("server_url")
        server_url_secret = body.get("server_url_secret")

    if not agent_id_str and not server_url:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="agent_id or server_url required in requestData",
        )

    # Validate agent_id shape here so a malformed value is a clean 400, not a
    # ValueError -> 500 when _resolve_agent does UUID(agent_id_str).
    if agent_id_str:
        try:
            UUID(agent_id_str)
        except (ValueError, TypeError) as exc:
            raise ApiError(
                status_code=400,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"agent_id must be a UUID, got {agent_id_str!r}",
            ) from exc

    # Validate server_url is HTTPS in production
    settings = get_settings()
    if server_url and settings.is_production and not server_url.startswith("https://"):
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="server_url must use HTTPS in production",
        )

    # Resolve agent config
    (
        config,
        agent_id_for_call,
        template_variables,
        request_metadata,
        knowledge_context,
    ) = await _resolve_agent(session, auth, agent_id_str, server_url, server_url_secret)

    # Apply template variables and knowledge context
    if template_variables:
        from turncall.services.template_renderer import render_agent_config

        config = render_agent_config(config, template_variables)
    if knowledge_context:
        from turncall.services.template_renderer import prepend_knowledge_context

        config = prepend_knowledge_context(config, knowledge_context)

    engine = get_engine()
    session_factory = create_session_factory(engine)

    # Create call record
    from turncall.domain.enums import CallDirection, CallEventType
    from turncall.storage.repositories import call_repo

    call_metadata: dict[str, Any] = {
        "transport": "webrtc",
        **({"template_variables": template_variables} if template_variables else {}),
        **({"metadata": request_metadata} if request_metadata else {}),
        **({"knowledge_context": knowledge_context} if knowledge_context else {}),
    }

    async with session_factory() as db_session:
        call = await call_repo.create_call(
            db_session,
            project_id=auth.project_id,
            direction=CallDirection.INBOUND,
            active_agent_id=agent_id_for_call,
            metadata_json=call_metadata,
        )

        init_seq = await call_repo.get_next_sequence_number(db_session, call.id)
        await call_repo.create_call_event(
            db_session,
            call_id=call.id,
            event_type=CallEventType.CALL_INITIALIZING,
            payload={"direction": "inbound", "transport": "webrtc"},
            sequence_number=init_seq,
        )
        await db_session.commit()

    call_context = CallContext(
        call_id=call.id,
        project_id=auth.project_id,
        agent_id=agent_id_for_call or UUID(int=0),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=session_factory,
    )

    # Use SmallWebRTCRequestHandler — it creates/reuses the connection
    async def on_connection(connection: SmallWebRTCConnection) -> None:
        """Callback invoked when a new WebRTC connection is established."""
        # Avatar is WebRTC + cascade only. Skip-with-warning on s2s.
        avatar_on = config.avatar.enabled and config.pipeline_mode == "cascade"
        if config.avatar.enabled and config.pipeline_mode != "cascade":
            logger.warning("Avatar requested on non-cascade pipeline; skipping")

        transport = create_whatsapp_transport(connection, video_out=avatar_on)

        try:
            call_session = await build_call_pipeline(
                config=config,
                transport=transport,
                call_context=call_context,
                settings=settings,
                session_factory=session_factory,
                audio_sample_rate=16000,
                avatar_enabled=avatar_on,
            )
        except Exception:
            # The pipeline never started, so call_session's finalize-on-exit
            # won't run — mark the call FAILED here or it's stuck at 'initiated'
            # forever (e.g. a provider misconfig raising during service build).
            logger.exception("webrtc pipeline build failed", call_id=str(call.id))
            await _finalize_failed(session_factory, call.id)
            raise
        asyncio.create_task(call_session.start())  # noqa: RUF006

    answer = await _request_handler.handle_web_request(webrtc_request, on_connection)

    if answer is None:
        raise ApiError(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Failed to create WebRTC answer",
        )

    logger.info("WebRTC call started: call={call_id}", call_id=str(call.id))
    return JSONResponse(content=answer)


@router.patch("/connect", status_code=200)
async def webrtc_ice_candidate(request: Request) -> JSONResponse:
    """WebRTC signaling endpoint — ICE candidate trickle.

    Accepts ICE candidates and forwards them to the peer connection.
    """
    body = await request.json()

    candidates = [
        IceCandidate(
            candidate=c.get("candidate", ""),
            sdp_mid=c.get("sdpMid", c.get("sdp_mid", "")),
            sdp_mline_index=c.get("sdpMLineIndex", c.get("sdp_mline_index", 0)),
        )
        for c in body.get("candidates", [])
    ]
    patch_request = SmallWebRTCPatchRequest(
        pc_id=body.get("pc_id", ""),
        candidates=candidates,
    )

    await _request_handler.handle_patch_request(patch_request)
    return JSONResponse(content={"status": "ok"})


async def _resolve_agent(
    session: Any,
    auth: Auth,
    agent_id_str: str | None,
    server_url: str | None,
    server_url_secret: str | None,
) -> tuple[AgentConfig, UUID | None, dict, dict, str | None]:
    """Resolve agent config from agent_id or server_url.

    Returns (config, agent_id, template_variables, metadata, knowledge_context).
    """
    agent = None
    dynamic_config = None
    template_variables: dict[str, str] = {}
    request_metadata: dict = {}
    knowledge_context: str | None = None

    if agent_id_str:
        agent_id = UUID(agent_id_str)
        agent = await agent_repo.get_agent_by_id(
            session, agent_id, project_id=auth.project_id
        )
        if agent is None:
            raise NotFoundError("Agent", str(agent_id))
    elif server_url:
        from uuid import uuid4

        from turncall.events.server_events import send_call_init
        from turncall.services.call_init_resolver import resolve_call_init

        temp_call_id = str(uuid4())
        response = await send_call_init(
            server_url,
            call_id=temp_call_id,
            from_number="",
            to_number="",
            transport_type="webrtc",
            secret=server_url_secret,
        )
        if response.success and response.data:
            result = await resolve_call_init(session, response.data)
            agent = result.agent
            dynamic_config = result.dynamic_config
            template_variables = result.template_variables
            request_metadata = result.metadata
            knowledge_context = result.knowledge_context

        if agent is None and dynamic_config is None:
            raise ApiError(
                status_code=502,
                code=ErrorCode.INTERNAL_ERROR,
                message="server_url did not return a valid agent",
            )

    if dynamic_config:
        config = AgentConfig.model_validate(dynamic_config)
    else:
        config = AgentConfig.model_validate(agent.config_blob)

    agent_id_for_call = agent.id if agent else None
    return (
        config,
        agent_id_for_call,
        template_variables,
        request_metadata,
        knowledge_context,
    )
