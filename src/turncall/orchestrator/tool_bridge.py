"""Bridge between TurnCall tool definitions and Pipecat LLM function calling.

Converts ToolDefinition objects from AgentConfig into registered
function handlers on Pipecat's LLM service. Built-in tools delegate
to the shared call_control service for consistent behavior with
the live call control API.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.domain.models import ToolDefinition
from turncall.events.webhook_signing import sign_payload
from turncall.services import call_control

if TYPE_CHECKING:
    from pipecat.services.llm_service import LLMService

    from turncall.orchestrator.pipeline_factory import CallContext


BUILTIN_TOOL_NAMES = frozenset(
    {
        "end_call",
        "transfer_call",
        "handoff_to_agent",
        "send_dtmf",
    }
)

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
) -> None:
    """Persist the tool invocation + call event and dispatch tool.result to
    webhook subscribers. Runs in the background; failures are logged, not raised."""
    payload = {"tool_name": function_name, "arguments": args, "result": result}
    try:
        async with call_context.session_factory() as session:
            from turncall.domain.enums import CallEventType
            from turncall.storage.repositories import call_repo, tool_invocation_repo

            await tool_invocation_repo.create_invocation(
                session,
                call_id=call_context.call_id,
                tool_name=function_name,
                input_json=args,
                status="succeeded",
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
    if not tool_def.webhook_url:
        return '{"error": "No webhook URL configured"}'

    payload = {
        "tool_name": tool_def.name,
        "arguments": args,
        "call_id": str(call_context.call_id),
        "project_id": str(call_context.project_id),
    }
    # Sign over the exact bytes sent so the receiver can verify them verbatim.
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if tool_def.webhook_secret:
        signature, ts = sign_payload(body, tool_def.webhook_secret)
        headers["X-TurnCall-Signature"] = signature
        headers["X-TurnCall-Timestamp"] = str(ts)

    client = get_http_client()
    try:
        response = await client.post(
            tool_def.webhook_url,
            content=body,
            headers=headers,
            timeout=tool_def.timeout_seconds,
        )
        response.raise_for_status()
        return response.text
    except httpx.TimeoutException:
        logger.warning(
            "tool_webhook_timeout",
            tool=tool_def.name,
            timeout=tool_def.timeout_seconds,
        )
        return '{"error": "Tool execution timed out"}'
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "tool_webhook_error",
            tool=tool_def.name,
            status=exc.response.status_code,
        )
        return f'{{"error": "Tool returned status {exc.response.status_code}"}}'


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

        # Build new system message for the target agent
        new_messages = []
        if config.system_prompt:
            new_messages.append({"role": "system", "content": config.system_prompt})

        # Reset the LLM context to the new agent's prompt
        context = params.context
        context.set_messages(new_messages)

        logger.info(
            "handoff_context: switched LLM context to agent '{name}'",
            name=target.name,
        )

    except Exception:
        logger.exception("handoff_context: failed to switch context")


def register_tools(
    llm: LLMService,
    tools: list[ToolDefinition],
    call_context: CallContext,
) -> None:
    """Register tool definitions as LLM function handlers."""
    registered = 0
    for tool_def in tools:
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

        if function_name in BUILTIN_TOOL_NAMES:
            result = await _execute_builtin(function_name, args, call_context)
        elif (
            call_context.mcp_manager is not None
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
        await params.result_callback(result)
        _spawn(_log_tool_result(call_context, function_name, args, result))

    llm.register_function(tool_def.name, handler)
