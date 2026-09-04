"""Shared call-pipeline assembly for every voice transport.

Twilio (media_stream), WhatsApp, and WebRTC all did the same thing once their
transport existed: load the agent's KB attachments, `create_pipeline(...)`,
register tools on the LLM service, and construct a `CallSession`. That block was
triplicated and had drifted before (the KB-attachments wiring had to be fixed in
three places — review finding #1). This centralizes it: callers pass their
transport plus the transport-specific options and get back a ready `CallSession`
to start however suits them (await it in a blocking WS handler, or wrap it in a
task from a fire-and-forget connection callback).
"""

from __future__ import annotations

from typing import Any

from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.call_session import CallSession
from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline
from turncall.orchestrator.tool_bridge import register_tools


async def build_call_pipeline(
    *,
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    settings: Settings,
    session_factory: Any,
    audio_sample_rate: int = 8000,
    mcp_tools: list[Any] | None = None,
    avatar_enabled: bool = False,
) -> CallSession:
    """Assemble the pipeline (KB retrieval + tools wired in) and return an
    unstarted CallSession. audio_sample_rate defaults to 8000 (Twilio mulaw);
    WebRTC/WhatsApp pass 16000. mcp_tools/avatar are transport-specific."""
    from turncall.orchestrator.knowledge_processor import load_agent_kb_attachments
    from turncall.services.retrieval import build_knowledge_preamble

    kb_attachments = await load_agent_kb_attachments(
        session_factory, call_context.agent_id
    )
    # Prompt-mode full text + an awareness hint for auto/tool KBs, injected into
    # the system prompt (create_pipeline is sync, so we build it here).
    knowledge_preamble = await build_knowledge_preamble(
        session_factory, kb_attachments or []
    )

    pipeline = create_pipeline(
        config=config,
        transport=transport,
        call_context=call_context,
        openai_api_key=settings.openai.api_key,
        pipecat_settings=settings.pipecat,
        audio_sample_rate=audio_sample_rate,
        byom_settings=settings.byom,
        google_api_key=settings.google.api_key,
        anthropic_api_key=settings.anthropic.api_key,
        openrouter_api_key=settings.openrouter.api_key,
        knowledge_base_attachments=kb_attachments or None,
        knowledge_preamble=knowledge_preamble,
        mcp_tools=mcp_tools or None,
        avatar_enabled=avatar_enabled,
    )

    # Register static + MCP tools on the LLM service.
    all_tools = list(config.tools) + list(mcp_tools or [])
    if all_tools:
        from pipecat.services.llm_service import LLMService

        for proc in pipeline.processors_with_metrics():
            if isinstance(proc, LLMService):
                register_tools(proc, all_tools, call_context)
                break

    return CallSession(
        call_context=call_context,
        transport=transport,
        pipeline=pipeline,
        first_message=config.first_message,
        pipeline_mode=config.pipeline_mode,
    )
