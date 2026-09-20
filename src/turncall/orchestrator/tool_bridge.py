"""Bridge between TurnCall tool definitions and Pipecat LLM function calling.

Converts ToolDefinition objects from AgentConfig into registered
function handlers on Pipecat's LLM service. Built-in tools delegate
to the shared call_control service for consistent behavior with
the live call control API.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger

from turncall.domain.enums import ToolExecutionMode
from turncall.domain.models import BUILTIN_TOOL_NAMES, ToolDefinition
from turncall.services import call_control
from turncall.services.tool_mocks import intercept
from turncall.services.tool_webhook import classify_tool_result, post_tool_webhook

if TYPE_CHECKING:
    from pipecat.services.llm_service import LLMService

    from turncall.orchestrator.pipeline_factory import CallContext


# Background logging/dispatch tasks kept referenced so asyncio (which holds only
# a weak ref) doesn't GC them mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:  # type: ignore[no-untyped-def]
    """Run a coroutine off the tool-result critical path."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _log_tool_result(
    call_context: CallContext,
    function_name: str,
    args: dict[str, Any],
    result: str,
    latency_ms: int | None = None,
) -> None:
    """Persist the tool invocation + call event and dispatch tool.result to
    webhook subscribers. Runs in the background; failures are logged, not raised."""
    if call_context.is_eval:
        # No `calls` row behind an eval (ADR-0018), so `tool_invocations.call_id`
        # has nothing to point at and the write is a foreign-key error every
        # time. The iteration's tool record is `ToolMocks.calls` instead, which
        # is what reaches the run's results.
        return
    payload = {"tool_name": function_name, "arguments": args, "result": result}
    try:
        async with call_context.session_factory() as session:
            from turncall.domain.enums import CallEventType
            from turncall.storage.repositories import call_repo, tool_invocation_repo

            status, output_json = classify_tool_result(result)
            await tool_invocation_repo.create_invocation(
                session,
                call_id=call_context.call_id,
                tool_name=function_name,
                input_json=args,
                status=status,
                output_json=output_json,
                latency_ms=latency_ms,
            )
            seq = await call_repo.get_next_sequence_number(
                session, call_context.call_id
            )
            await call_repo.create_call_event(
                session,
                call_id=call_context.call_id,
                event_type=CallEventType.TOOL_RESULT,
                payload=payload,
                sequence_number=seq,
            )
            await session.commit()

            from turncall.events.dispatcher import dispatch_event

            await dispatch_event(
                session,
                project_id=call_context.project_id,
                event_type=CallEventType.TOOL_RESULT,
                payload=payload,
                call_id=call_context.call_id,
            )
    except Exception:
        logger.exception("tool_event_log_error")


async def _execute_webhook_tool(
    tool_def: ToolDefinition,
    args: dict[str, Any],
    call_context: CallContext,
) -> str:
    """Execute a webhook-based tool by POSTing to the configured URL."""
    return await post_tool_webhook(
        tool_def,
        args,
        project_id=call_context.project_id,
        call_id=call_context.call_id,
    )


async def _execute_builtin(
    function_name: str,
    args: dict[str, Any],
    call_context: CallContext,
) -> str:
    """Execute a built-in tool using shared call_control primitives."""
    async with call_context.session_factory() as session:
        if function_name == "end_call":
            result = await call_control.end_call(
                session,
                call_context.call_id,
                reason=args.get("reason", "assistant_ended"),
            )
        elif function_name == "transfer_call":
            result = await call_control.transfer_call(
                session,
                call_context.call_id,
                target_number=args.get("target_number", ""),
                transfer_mode=args.get("transfer_mode", "cold"),
                transfer_message=args.get("transfer_message"),
                briefing=args.get("briefing"),
                fallback_message=args.get("fallback_message"),
                reason=args.get("reason"),
            )
        elif function_name == "handoff_to_agent":
            target_id = args.get("agent_id") or args.get("assistant_id") or ""
            logger.info(
                "handoff_to_agent: target_id={target_id} args={args}",
                target_id=target_id,
                args=args,
            )
            resolved_target = UUID(target_id) if target_id else call_context.agent_id
            result = await call_control.handoff_to_agent(
                session,
                call_context.call_id,
                target_agent_id=resolved_target,
                reason=args.get("reason"),
                context_payload=args.get("context"),
            )
            logger.info(
                "handoff_to_agent: result success={success} message={msg}",
                success=result.success,
                msg=result.message,
            )
        elif function_name == "send_dtmf":
            result = await call_control.send_dtmf(
                session,
                call_context.call_id,
                digits=args.get("digits", ""),
            )
        else:
            return json.dumps({"error": f"Unknown built-in tool: {function_name}"})

        await session.commit()
        return json.dumps(
            {
                "success": result.success,
                "message": result.message,
                "details": result.details,
            }
        )


async def _apply_handoff_context(
    args: dict[str, Any],
    call_context: CallContext,
    params: Any,
) -> None:
    """Switch the LLM context to the target agent's system prompt.

    Loads the target agent's config from DB and resets the conversation
    context so the LLM operates as the new agent from this point on.
    """
    from turncall.domain.models import AgentConfig

    target_id = args.get("agent_id") or args.get("assistant_id") or ""
    if not target_id:
        return

    try:
        async with call_context.session_factory() as session:
            from turncall.storage.repositories import agent_repo

            target = await agent_repo.get_agent_by_id(session, UUID(target_id))
            if target is None:
                logger.warning("handoff_context: target agent not found")
                return

            config = AgentConfig.model_validate(target.config_blob)

        # The prompt lives on the LLM service, not in the context, so switching
        # agents is a settings update rather than a rewritten first message.
        # Doing it the old way now would send both prompts: the OpenAI adapter
        # prepends system_instruction to the context messages, so the previous
        # agent's instructions would survive the handoff.
        from pipecat.frames.frames import LLMUpdateSettingsFrame
        from pipecat.services.llm_service import LLMSettings

        from turncall.orchestrator.pipeline_factory import (
            _build_system_instruction,
            _build_tools_schema,
        )

        instruction = _build_system_instruction(config)

        # Clear the conversation as before: the new agent starts fresh.
        params.context.set_messages([])
        await params.pipeline_worker.queue_frame(
            LLMUpdateSettingsFrame(delta=LLMSettings(system_instruction=instruction))
        )

        if config.mcp_servers:
            # MCP sessions belong to the agent the call started as — they
            # aren't torn down and re-opened mid-call, so the target's servers
            # stay unconnected. Better said out loud than discovered.
            logger.warning(
                "handoff_context: target agent's mcp servers are not connected "
                "mid-call: {servers}",
                servers=[s.name for s in config.mcp_servers],
            )

        # Tools live in two places: the handler registry and the advertised
        # schema. Moving only the prompt left the model believing it was the
        # new agent while still holding the previous one's tools, and none of
        # its own. NOT_GIVEN clears the set when the target defines none.
        from pipecat.frames.frames import LLMSetToolsFrame
        from pipecat.processors.aggregators.llm_context import NOT_GIVEN

        if config.tools:
            register_tools(params.llm, list(config.tools), call_context)
        tools_schema = _build_tools_schema(config)
        await params.pipeline_worker.queue_frame(
            LLMSetToolsFrame(
                tools=tools_schema if tools_schema is not None else NOT_GIVEN
            )
        )

        logger.info(
            "handoff_context: switched system instruction to agent '{name}'",
            name=target.name,
        )

    except Exception:
        logger.exception("handoff_context: failed to switch context")


def register_tools(
    llm: LLMService,
    tools: list[ToolDefinition],
    call_context: CallContext,
) -> None:
    """Register tool definitions as LLM function handlers.

    Deduplicated by name, first wins — the same rule and the same order
    `_build_tools_schema` applies when it decides what to advertise. Pipecat's
    registry is keyed by name, so without this a duplicate silently replaced
    the handler for a name the schema had already given to someone else.
    """
    registered = 0
    seen: set[str] = set()
    for tool_def in tools:
        if tool_def.name in seen:
            logger.warning("tool_registration_collision_skipped", tool=tool_def.name)
            continue
        seen.add(tool_def.name)
        try:
            _register_single_tool(llm, tool_def, call_context)
            registered += 1
        except Exception:
            logger.exception("Failed to register tool: {name}", name=tool_def.name)
    logger.info("Tools registered: {n}/{total}", n=registered, total=len(tools))


def _register_single_tool(
    llm: LLMService,
    tool_def: ToolDefinition,
    call_context: CallContext,
) -> None:
    """Register a single tool on the LLM service."""
    from pipecat.services.llm_service import FunctionCallParams

    async def handler(params: FunctionCallParams) -> None:
        function_name = params.function_name
        args = params.arguments

        logger.info(
            "Tool called: {tool} for call {call_id}",
            tool=function_name,
            call_id=str(call_context.call_id),
        )

        # An eval's mocks short-circuit ahead of every branch below: a built-in
        # acts on a call that does not exist, a webhook books a real
        # appointment, an MCP server is a third party's. `intercept` returns
        # None on a real call (no mocks) and under `live`, so this costs a
        # dict lookup there. #71.
        mocked = intercept(call_context.tool_mocks, function_name, args)
        if mocked is not None:
            await params.result_callback(mocked)
            return

        started = time.perf_counter()
        if function_name in BUILTIN_TOOL_NAMES:
            result = await _execute_builtin(function_name, args, call_context)
        elif (
            # Only when this registration is not itself a webhook tool. Asking
            # the manager by name alone sent a name the agent config had
            # already claimed to the MCP server instead: the model was shown
            # the customer's description and schema, and something else ran.
            # _build_tools_schema gives static tools the name, so dispatch has
            # to agree. chat_tools._run makes the same check.
            not tool_def.webhook_url
            and call_context.mcp_manager is not None
            and call_context.mcp_manager.is_mcp_tool(function_name)
        ):
            result = await call_context.mcp_manager.call_tool(function_name, args)
        else:
            result = await _execute_webhook_tool(tool_def, args, call_context)

        # For handoff: switch the LLM context to the new agent's prompt
        if function_name == "handoff_to_agent":
            await _apply_handoff_context(args, call_context, params)

        # Hand the result back to the LLM immediately — the model is waiting on
        # this to continue speaking. The invocation record + tool.result webhook
        # dispatch (which can retry ~90s against a dead subscriber) run off the
        # critical path so they never sit between the caller and the response.
        latency_ms = int((time.perf_counter() - started) * 1000)
        await params.result_callback(result)
        if call_context.tool_mocks is not None:
            # A `live` eval: the tool really ran, and the run's record says so.
            call_context.tool_mocks.record(function_name, args, result, mocked=False)
        _spawn(_log_tool_result(call_context, function_name, args, result, latency_ms))

    # execution_mode="async" means the call outlives an interruption: its
    # result is delivered whenever it arrives instead of being cancelled the
    # moment the caller talks over the agent. Pipecat spells that
    # cancel_on_interruption=False, and its own _function_is_async() reads the
    # same flag back. Sync stays the default — a built-in acting on the call
    # itself must not survive the caller changing their mind.
    llm.register_function(
        tool_def.name,
        handler,
        cancel_on_interruption=tool_def.execution_mode != ToolExecutionMode.ASYNC,
    )
