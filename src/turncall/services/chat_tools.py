"""Tool calling for text sessions — SMS, the Chat API and WhatsApp text.

The voice pipeline registers tools through orchestrator/tool_bridge, which
needs a CallContext: every built-in it dispatches resolves through
call_control against a live call_id. A text session has a session_id and no
call, so it gets the customer's webhook tools plus anything discovered from
their MCP servers, and none of the built-ins.

MCP sessions are opened per inbound message and closed with `aclose()`. That
costs a handshake per reply, but a text request is a single task start to
finish, so connect and close land in the same one — which is what MCP's anyio
cancel scopes require.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    AgentConfig,
    ToolDefinition,
)
from turncall.services.tool_webhook import classify_tool_result, post_tool_webhook

# Recording tasks kept referenced so asyncio (which holds only a weak ref)
# can't GC one mid-write.
_RECORDING: set[asyncio.Task] = set()


async def _record_invocation(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    latency_ms: int,
    *,
    session_id: UUID,
    project_id: UUID,
) -> None:
    """Persist the invocation and dispatch tool.result, as the voice path does.

    Runs off the reply's critical path: the row is small but the webhook
    dispatch can retry for ~90s against a dead subscriber, and the customer is
    waiting on a message. Failures are logged, never raised — losing the audit
    row must not cost someone their reply.
    """
    from turncall.domain.enums import CallEventType
    from turncall.events.dispatcher import dispatch_event
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import tool_invocation_repo

    status, output_json = classify_tool_result(result)
    try:
        async with get_session_factory()() as db:
            await tool_invocation_repo.create_invocation(
                db,
                session_id=session_id,
                tool_name=tool_name,
                input_json=args,
                status=status,
                output_json=output_json,
                latency_ms=latency_ms,
            )
            await db.commit()

            await dispatch_event(
                db,
                project_id=project_id,
                event_type=CallEventType.TOOL_RESULT,
                payload={
                    "tool_name": tool_name,
                    "arguments": args,
                    "result": result,
                },
                session_id=session_id,
            )
    except Exception:
        logger.exception("chat_tool_record_failed", tool=tool_name)


@dataclass(frozen=True)
class ChatTools:
    """What a text turn needs to call tools: the schemas to advertise, an
    executor to run them, and the cleanup for any MCP sessions opened."""

    schemas: list[dict[str, Any]]
    execute: Callable[[str, dict[str, Any]], Awaitable[str]]
    aclose: Callable[[], Awaitable[None]]


def _to_function_schema(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters_schema or {"type": "object", "properties": {}},
    }


async def build_chat_tools(
    config: AgentConfig,
    *,
    session_id: UUID,
    project_id: UUID,
) -> ChatTools:
    """Assemble the tools available to one text turn."""
    webhook_tools = {
        t.name: t for t in config.tools if t.name not in BUILTIN_TOOL_NAMES
    }
    dropped = [t.name for t in config.tools if t.name in BUILTIN_TOOL_NAMES]
    if dropped:
        logger.debug("chat_tools_builtins_skipped", tools=dropped)

    mcp_manager: Any | None = None
    mcp_tools: list[ToolDefinition] = []
    if config.mcp_servers:
        from turncall.services.mcp_client import MCPSessionManager

        # No call, but the manager tags its requests with these — a session id
        # is the closest thing a text conversation has to a call id.
        mcp_manager = MCPSessionManager(call_id=session_id, project_id=project_id)
        try:
            mcp_tools = await mcp_manager.connect_servers(config.mcp_servers)
        except Exception:
            # One unreachable MCP server must not cost the customer their reply.
            logger.exception("chat_tools_mcp_connect_failed", session=str(session_id))
            mcp_tools = []

    # Static tools are the customer's own, so they win a name clash — same
    # precedence the voice path applies in _build_tools_schema.
    shadowed = {t.name for t in mcp_tools} & webhook_tools.keys()
    if shadowed:
        logger.warning("chat_tools_mcp_name_collision", tools=sorted(shadowed))

    schemas = [_to_function_schema(t) for t in webhook_tools.values()]
    schemas += [
        _to_function_schema(t) for t in mcp_tools if t.name not in webhook_tools
    ]

    async def _run(name: str, args: dict[str, Any]) -> str:
        if (
            mcp_manager is not None
            and name not in webhook_tools
            and mcp_manager.is_mcp_tool(name)
        ):
            return await mcp_manager.call_tool(name, args)

        tool = webhook_tools.get(name)
        if tool is None:
            if name in BUILTIN_TOOL_NAMES:
                return json.dumps(
                    {"error": f"{name} is only available on a voice call"}
                )
            return json.dumps({"error": f"Unknown tool: {name}"})

        return await post_tool_webhook(
            tool, args, project_id=project_id, session_id=session_id
        )

    async def execute(name: str, args: dict[str, Any]) -> str:
        started = time.perf_counter()
        result = await _run(name, args)
        latency_ms = int((time.perf_counter() - started) * 1000)

        task = asyncio.create_task(
            _record_invocation(
                name,
                args,
                result,
                latency_ms,
                session_id=session_id,
                project_id=project_id,
            )
        )
        _RECORDING.add(task)
        task.add_done_callback(_RECORDING.discard)
        return result

    async def aclose() -> None:
        if mcp_manager is not None:
            await mcp_manager.close()

    return ChatTools(schemas=schemas, execute=execute, aclose=aclose)
