"""Twilio Media Stream WebSocket endpoint.

Thin entry point that accepts a Twilio WebSocket connection,
resolves the agent configuration, and delegates to the
Pipecat-based voice pipeline via CallSession.
"""

import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.config import get_settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_builder import build_call_pipeline
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.storage.database import create_session_factory, get_engine

router = APIRouter()


async def _wait_for_start_event(websocket: WebSocket) -> dict | None:
    """Read WebSocket messages until we get a Twilio 'start' event."""
    while True:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        if data.get("event") == "start":
            return data
        if data.get("event") == "connected":
            continue
        logger.warning("unexpected_pre_start_event", event=data.get("event"))


@router.websocket("/ws/media-stream")
async def media_stream_websocket(websocket: WebSocket) -> None:
    """Twilio Media Stream WebSocket endpoint.

    Flow:
    1. Accept WebSocket from Twilio
    2. Wait for 'start' event to get call metadata
    3. Load agent config from DB
    4. Create Pipecat pipeline with TwilioFrameSerializer
    5. Run pipeline until disconnect
    """
    await websocket.accept()

    try:
        # Wait for Twilio's start event with call metadata
        start_data = await _wait_for_start_event(websocket)
        if start_data is None:
            return

        start = start_data.get("start", {})
        custom_params = start.get("customParameters", {})
        # Only callId is passed in TwiML. Resolve everything from the call record.
        call_id_str = custom_params.get("callId", "") or custom_params.get(
            "call_id", ""
        )
        stream_sid = start.get("streamSid", "")
        call_sid = start.get("callSid", "")

        logger.info(
            "media_stream_start: call={call_id} stream={stream_sid}",
            call_id=call_id_str,
            stream_sid=stream_sid,
        )

        if not call_id_str:
            logger.error("media_stream_missing_call_id")
            return

        settings = get_settings()
        engine = get_engine()
        session_factory = create_session_factory(engine)

        # Resolve everything from the call record (project_id, agent_id, config)
        async with session_factory() as session:
            from turncall.storage.repositories import agent_repo, call_repo

            call_row = await call_repo.get_call_by_id(session, UUID(call_id_str))
            if call_row is None:
                logger.error("media_stream_call_not_found: {id}", id=call_id_str)
                return

            project_id_str = str(call_row.project_id)
            agent_id_str = (
                str(call_row.active_agent_id) if call_row.active_agent_id else "dynamic"
            )
            config = None

            # Check for dynamic inline config in call metadata
            if call_row.metadata_json.get("dynamic_config"):
                dynamic = call_row.metadata_json["dynamic_config"]
                config = AgentConfig.model_validate(dynamic)
                logger.info("Using dynamic inline agent config")
            elif call_row.active_agent_id:
                agent = await agent_repo.get_agent_by_id(
                    session, call_row.active_agent_id
                )
                if agent is not None:
                    config = AgentConfig.model_validate(agent.config_blob)

            if config is None:
                logger.error(
                    "media_stream_no_config",
                    agent_id=agent_id_str,
                )
                return

            # Apply template variables if stored in call metadata
            template_variables = {}
            if call_row and call_row.metadata_json:
                template_variables = call_row.metadata_json.get(
                    "template_variables", {}
                )

            if template_variables:
                from turncall.services.template_renderer import render_agent_config

                config = render_agent_config(config, template_variables)
                logger.info(
                    "Template variables applied: {keys}",
                    keys=list(template_variables.keys()),
                )

            # Apply knowledge_context if stored in call metadata
            knowledge_context = (
                call_row.metadata_json.get("knowledge_context")
                if call_row.metadata_json
                else None
            )
            if knowledge_context:
                from turncall.services.template_renderer import prepend_knowledge_context

                config = prepend_knowledge_context(config, knowledge_context)
                logger.info("Knowledge context applied to system prompt")

        # Connect MCP servers and discover tools (if configured)
        mcp_manager = None
        mcp_tools: list = []
        if config.mcp_servers:
            from turncall.services.mcp_client import MCPSessionManager

            mcp_manager = MCPSessionManager(
                call_id=UUID(call_id_str),
                project_id=UUID(project_id_str),
            )
            mcp_tools = await mcp_manager.connect_servers(config.mcp_servers)

        # Build call context
        call_context = CallContext(
            call_id=UUID(call_id_str),
            project_id=UUID(project_id_str),
            agent_id=UUID(agent_id_str),
            call_sid=call_sid,
            stream_sid=stream_sid,
            session_factory=session_factory,
            mcp_manager=mcp_manager,
        )

        # Create Twilio transport via factory
        from turncall.orchestrator.transport_factory import create_twilio_transport

        transport = create_twilio_transport(websocket, stream_sid)

        call_session = await build_call_pipeline(
            config=config,
            transport=transport,
            call_context=call_context,
            settings=settings,
            session_factory=session_factory,
            mcp_tools=mcp_tools,
        )
        # WS handler stays open for the call — await (don't fire-and-forget).
        await call_session.start()

    except WebSocketDisconnect:
        logger.info("media_stream_disconnected")
    except Exception:
        logger.exception("media_stream_error")
