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

from loguru import logger

from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.call_session import CallSession, fail_unstarted_call
from turncall.orchestrator.config_warnings import knowledge_mode_warning
from turncall.orchestrator.pipeline_factory import (
    DYNAMIC_AGENT_ID,
    CallContext,
    create_pipeline,
)
from turncall.orchestrator.tool_bridge import register_tools
from turncall.services import drain
from turncall.services.credentials import (
    Credentials,
    MissingProviderKeyError,
    for_project,
)


async def load_call_credentials(
    session_factory: Any, project_id: Any, settings: Settings
) -> Credentials:
    """Every key this call can resolve, read fresh per call (ADR-0020).

    Read at build rather than cached, so a credential written or deleted
    between two calls is what the next one runs on, with no restart.
    """
    async with session_factory() as session:
        return await for_project(session, project_id, settings)


async def build_call_pipeline(
    *,
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    settings: Settings,
    session_factory: Any,
    **options: Any,
) -> CallSession:
    """Assemble the pipeline and return an unstarted CallSession.

    A build that raises — a provider SDK refusing an empty key, say — fails the
    call here, for every transport, with the reason and `call.ended`; the error
    still propagates to the caller.
    """
    try:
        return await _assemble(
            config=config,
            transport=transport,
            call_context=call_context,
            settings=settings,
            session_factory=session_factory,
            **options,
        )
    except Exception as exc:
        await fail_unstarted_call(call_context, exc)
        raise


async def _kb_attachments(
    config: AgentConfig, call_context: CallContext, session_factory: Any
) -> list[dict[str, Any]]:
    """The call's knowledge bases: its agent's links, or — with no agent row to
    link one to (ADR-0017) — the inline config's own, checked against the
    call's project (#260)."""
    from turncall.orchestrator.knowledge_processor import (
        load_agent_kb_attachments,
        load_inline_kb_attachments,
    )

    if call_context.agent_id == DYNAMIC_AGENT_ID:
        return await load_inline_kb_attachments(
            session_factory, call_context.project_id, config.knowledge_bases
        )
    return await load_agent_kb_attachments(session_factory, call_context.agent_id)


async def _assemble(
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
    """Assemble the pipeline (KB retrieval + tools wired in). audio_sample_rate
    defaults to 8000 (Twilio mulaw); WebRTC/WhatsApp pass 16000. mcp_tools/avatar
    are transport-specific."""
    from turncall.services.retrieval import build_knowledge_preamble

    kb_attachments = await _kb_attachments(config, call_context, session_factory)
    unsupported = knowledge_mode_warning(
        config, {a.get("mode") for a in kb_attachments}
    )
    if unsupported is not None:
        # Before the preamble: its awareness hint would tell the model it can
        # answer from a knowledge base it has no way to read (#254).
        logger.warning(unsupported["message"])
        kb_attachments = [
            a for a in kb_attachments if a.get("mode") not in unsupported["modes"]
        ]
    # Prompt-mode full text + an awareness hint for auto/tool KBs, injected into
    # the system prompt (create_pipeline is sync, so we build it here).
    knowledge_preamble = await build_knowledge_preamble(
        session_factory, kb_attachments or []
    )

    credentials = await load_call_credentials(
        session_factory, call_context.project_id, settings
    )
    # Checked before anything is built, so a call that cannot work says which
    # key it lacks rather than "Missing credentials" from one SDK or a 401
    # mid-call from another. Not in an eval: text mode never opens the speech
    # providers, and a run there must not need their keys.
    missing = credentials.missing_keys(
        config, has_knowledge_tool=any(a.get("mode") == "tool" for a in kb_attachments)
    )
    if missing and not call_context.is_eval:
        raise MissingProviderKeyError(missing)

    pipeline = create_pipeline(
        config=config,
        transport=transport,
        call_context=call_context,
        credentials=credentials,
        pipecat_settings=settings.pipecat,
        audio_sample_rate=audio_sample_rate,
        byom_settings=settings.byom,
        knowledge_base_attachments=kb_attachments or None,
        knowledge_preamble=knowledge_preamble,
        mcp_tools=mcp_tools or None,
        avatar_enabled=avatar_enabled,
    )

    # Register static + MCP tools on the LLM service — on every member of a
    # failover group (#222), or a backup would take over with no handlers.
    all_tools = list(config.tools) + list(mcp_tools or [])
    if all_tools:
        from pipecat.services.llm_service import LLMService

        from turncall.orchestrator.llm_failover import FailoverLLM

        for proc in pipeline.processors_with_metrics():
            if isinstance(proc, LLMService):
                register_tools(FailoverLLM.group_of(proc), all_tools, call_context)
                break

    return CallSession(
        call_context=call_context,
        transport=transport,
        pipeline=pipeline,
        first_message=config.first_message,
        pipeline_mode=config.pipeline_mode,
        s2s_provider=config.s2s.provider,
        max_call_duration_seconds=config.max_call_duration_seconds,
        idle_message=config.idle_message,
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
                # Unconditionally, cancellation included: the caller is blocked
                # on this event and nothing else will ever release it.
                ready.set()

            if session is not None:
                await session.start()
        finally:
            # Every path where CallSession.cleanup won't run — a build failure,
            # or cancellation before start() took ownership — and in the task
            # that opened the transports, which is what their cancel scopes
            # require. `except Exception` alone used to skip this entirely when
            # the build was cancelled.
            if session is None and mcp_manager is not None:
                await mcp_manager.close()

    async def _run_live() -> None:
        # Live until the session has finalized: a SIGTERM waits for it (#199).
        with drain.live_call(call_context.call_id, config.max_call_duration_seconds):
            await _run()

    task = asyncio.create_task(_run_live())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    await ready.wait()
    if failure is not None:
        raise failure
