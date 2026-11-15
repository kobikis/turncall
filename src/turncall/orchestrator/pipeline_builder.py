"""Shared call-pipeline assembly for every voice transport.

Twilio (media_stream), WhatsApp, and WebRTC all did the same thing once their
transport existed: load the agent's KB attachments, `create_pipeline(...)`,
register tools on the LLM service, and construct a `CallSession`. That block was
triplicated and had drifted before (the KB-attachments wiring had to be fixed in
three places — review finding #1). This centralizes it: callers pass their
transport plus the transport-specific options and get back a ready `CallSession`.
Running it — the drain count, the live-call slot, MCP, the mixer, the start —
is `call_runner.run_call`'s (#491).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from loguru import logger

from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.avatar import avatar_gate
from turncall.orchestrator.call_session import CallSession, fail_unstarted_call
from turncall.orchestrator.config_warnings import (
    knowledge_mode_warning,
    procedures_on_s2s_warning,
)
from turncall.orchestrator.pipeline_factory import (
    DYNAMIC_AGENT_ID,
    CallContext,
    create_pipeline,
)
from turncall.orchestrator.procedures import with_procedures
from turncall.orchestrator.tool_bridge import register_tools, remember_config_tools
from turncall.services.agent_acceptance import AcceptedAgent
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
    between two calls is what the next one runs on, with no restart. With no
    database (the demo) there is no project either: the platform's keys alone.
    """
    if session_factory is None:
        return Credentials.from_settings(settings)
    async with session_factory() as session:
        return await for_project(session, project_id, settings)


async def build_call_pipeline(
    *,
    agent: AcceptedAgent,
    transport: Any,
    call_context: CallContext,
    settings: Settings,
    session_factory: Any,
    **options: Any,
) -> CallSession:
    """Assemble the pipeline and return an unstarted CallSession.

    `agent` is agent acceptance's answer (#481): a config nothing checked
    cannot reach a pipeline, and the build holds no copy of a rule — the
    provider rules and the procedures graph rule are acceptance's alone.

    A build that raises — a provider SDK refusing an empty key, say — fails the
    call here, for every transport, with the reason and `call.ended`; the error
    still propagates to the caller.
    """
    try:
        return await _assemble(
            config=agent.config,
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

    if not call_context.has_database:
        # Nothing to resolve a knowledge base against (the demo, which refuses
        # an agent naming one before any call).
        return []
    if call_context.agent_id == DYNAMIC_AGENT_ID:
        return await load_inline_kb_attachments(
            session_factory, call_context.project_id, config.knowledge_bases
        )
    return await load_agent_kb_attachments(session_factory, call_context.agent_id)


async def _with_skills(
    config: AgentConfig,
    call_context: CallContext,
    *,
    knowledge_preamble: str,
    mcp_tools: list[Any] | None,
    kb_attachments: list[dict[str, Any]],
    embedding_key: str,
) -> tuple[AgentConfig, CallContext]:
    """The config the call runs with its agent's skills (#424), and the call's
    context carrying them for `load_skill` — or both as given, for an agent
    with none (no I/O, nothing changed): its context's `CallSkills` stays the
    empty one every context starts with, for a handoff to fill (#427)."""
    from turncall.orchestrator.pipeline_factory import switch_inputs
    from turncall.orchestrator.skill_loader import (
        CallSkills,
        record_resolution,
        resolve_call_skills,
    )

    available = await resolve_call_skills(config, call_context)
    if not available:
        return config, call_context
    # Once, here: the call runs these versions whatever is published after.
    await record_resolution(call_context, available)
    skills = CallSkills(
        base=config,
        available=available,
        switch=switch_inputs(
            config,
            knowledge_preamble=knowledge_preamble,
            mcp_tools=mcp_tools,
            knowledge_base_attachments=kb_attachments or None,
        ),
        # What `query_knowledge` searches besides a loaded skill's KBs (#429).
        own_knowledge=tuple(a for a in kb_attachments if a.get("mode") == "tool"),
        embedding_key=embedding_key,
    )
    logger.info(
        "skills_resolved: {skills}",
        skills=[f"{s.name} v{s.version}" for s in available],
    )
    return skills.config(), replace(call_context, skills=skills)


def _log_skipped_procedures(config: AgentConfig) -> None:
    """On S2S no procedure runs (`with_procedures`), which agent acceptance
    answered as a warning: said here, where it is skipped. The graph rule
    itself is acceptance's (#444, #481)."""
    if (ignored := procedures_on_s2s_warning(config)) is not None:
        logger.warning("procedures_skipped: {message}", message=ignored["message"])


async def _assemble(
    *,
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    settings: Settings,
    session_factory: Any,
    audio_sample_rate: int = 8000,
    mcp_tools: list[Any] | None = None,
    carries_video: bool = False,
) -> CallSession:
    """Assemble the pipeline (KB retrieval + tools wired in). audio_sample_rate
    defaults to 8000 (Twilio mulaw); WebRTC/WhatsApp pass 16000. mcp_tools are
    transport-specific; carries_video is a browser call's, the only one that
    can show an Avatar."""
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
        session_factory, kb_attachments or [], [t.name for t in config.tools]
    )

    credentials = await load_call_credentials(
        session_factory, call_context.project_id, settings
    )

    _log_skipped_procedures(config)
    # Before the skills: a skill load rebuilds the tools from the config
    # they start from, which must keep move_to_step.
    config, call_context = with_procedures(config, call_context)
    config, call_context = await _with_skills(
        config,
        call_context,
        knowledge_preamble=knowledge_preamble,
        mcp_tools=mcp_tools,
        kb_attachments=kb_attachments,
        embedding_key=credentials.api_key("openai"),
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

    # After the key check: a call that fails there never shows anything.
    avatar_enabled = await avatar_gate(
        config, call_context, carries_video=carries_video
    )

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
                group = FailoverLLM.group_of(proc)
                register_tools(group, all_tools, call_context)
                # What a later switch may take away: these, never MCP's.
                remember_config_tools(group, config.tools)
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
