"""The shared media entry point (ADR-0022).

Each carrier owns its inbound edge — its webhook routes, its signature check,
its answer format, and the handshake its media stream opens with
(`twilio_media_stream.py` for Twilio). Every one of them ends by handing the
open stream to `run_media_stream`, which resolves the call's agent from the
call record and builds the pipeline. The serializer is chosen here from the
call's own carrier, in `create_carrier_transport`; past it, nothing learns
which carrier the call is on.
"""

from uuid import UUID

from fastapi import WebSocket
from loguru import logger

from turncall.config import get_settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_builder import build_call_pipeline
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
from turncall.orchestrator.transport_factory import (
    CARRIER_MEDIA,
    CarrierStream,
    create_carrier_transport,
)
from turncall.storage.database import create_session_factory, get_engine


async def run_media_stream(
    websocket: WebSocket, call_id: UUID, stream: CarrierStream
) -> None:
    """Run one call's pipeline on an accepted, started carrier media stream.

    Returns when the call ends. Raises what the pipeline raises; the carrier's
    edge owns the socket and decides what a failure means to it.
    """
    call_id_str = str(call_id)
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

        carrier = call_row.provider
        if carrier not in CARRIER_MEDIA:
            # A call reaching a media stream this deployment has no serializer
            # for: decoding it as some other carrier's would be noise.
            logger.error(
                "media_stream_unknown_carrier", call_id=call_id_str, carrier=carrier
            )
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
            agent = await agent_repo.get_agent_by_id(session, call_row.active_agent_id)
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
            template_variables = call_row.metadata_json.get("template_variables", {})

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
            from turncall.services.template_renderer import (
                prepend_knowledge_context,
            )

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
        agent_id=call_row.active_agent_id or DYNAMIC_AGENT_ID,
        # A stream that does not name the call's REST handle (Asterisk's)
        # leaves it to the call record, which always has it.
        call_sid=stream.call_sid or call_row.provider_call_sid or "",
        stream_sid=stream.stream_id,
        session_factory=session_factory,
        mcp_manager=mcp_manager,
    )

    from turncall.orchestrator.ambience import build_ambience_mixer

    mixer = await build_ambience_mixer(
        config,
        project_id=call_context.project_id,
        sample_rate=CARRIER_MEDIA[carrier].sample_rate,
        session_factory=session_factory,
        settings=settings,
    )
    # The serializer comes from the call's carrier, not from which edge
    # handed the stream over (ADR-0022).
    transport = create_carrier_transport(carrier, websocket, stream, mixer=mixer)

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
