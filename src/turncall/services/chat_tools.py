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

import json
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
from turncall.services.tool_webhook import post_tool_webhook


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

    schemas = [_to_function_schema(t) for t in webhook_tools.values()]
    schemas += [_to_function_schema(t) for t in mcp_tools]

    async def execute(name: str, args: dict[str, Any]) -> str:
        if mcp_manager is not None and mcp_manager.is_mcp_tool(name):
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

    async def aclose() -> None:
        if mcp_manager is not None:
            await mcp_manager.close()

    return ChatTools(schemas=schemas, execute=execute, aclose=aclose)
