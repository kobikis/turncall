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

import asyncio
from dataclasses import replace
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


# Spawned pipeline tasks kept referenced so asyncio (which holds only a weak
# ref) can't GC a running call.
_RUNNING: set[asyncio.Task] = set()


async def start_call_pipeline(
    *,
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    settings: Settings,
    session_factory: Any,
    **build_kwargs: Any,
) -> None:
    """Connect MCP servers, build the pipeline and run it — all in one task.

    For the fire-and-forget callers (WebRTC, WhatsApp voice), which return from
    a connection callback while the call keeps going. They can't do what
    media_stream does — connect inline, then `await start()` — because an MCP
    transport opens anyio cancel scopes that must be exited in the task that
    entered them, and `CallSession` closes the manager during its own cleanup.
    Connecting in the caller's task would therefore blow up at hangup.

    Returns once the pipeline is built, so a build failure still reaches the
    caller (WebRTC turns it into a 500); the run continues in the background.
    """
    ready = asyncio.Event()
    failure: BaseException | None = None

    async def _run() -> None:
        nonlocal failure
        mcp_manager: Any | None = None
        session: CallSession | None = None
        try:
            mcp_tools: list[Any] = []
            if config.mcp_servers:
                from turncall.services.mcp_client import MCPSessionManager

                mcp_manager = MCPSessionManager(
                    call_id=call_context.call_id,
                    project_id=call_context.project_id,
                )
                mcp_tools = await mcp_manager.connect_servers(config.mcp_servers)

            session = await build_call_pipeline(
                config=config,
                transport=transport,
                call_context=replace(call_context, mcp_manager=mcp_manager),
                settings=settings,
                session_factory=session_factory,
                mcp_tools=mcp_tools or None,
                **build_kwargs,
            )
        except Exception as exc:
            failure = exc
        finally:
            ready.set()

        if session is None:
            # CallSession.cleanup never runs on this path, so close the MCP
            # sessions here — in the task that opened them.
            if mcp_manager is not None:
                await mcp_manager.close()
            return

        await session.start()

    task = asyncio.create_task(_run())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    await ready.wait()
    if failure is not None:
        raise failure
