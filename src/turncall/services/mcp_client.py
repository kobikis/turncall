"""MCP client service — connect to MCP servers, discover and call tools.

Manages per-call MCP sessions. Supports stdio, SSE, and streamable HTTP
transports. Tools discovered from MCP servers are converted to TurnCall
ToolDefinition format and registered alongside webhook/builtin tools.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from loguru import logger
from mcp.client.session import ClientSession
from mcp.types import CallToolResult, Tool

from turncall.domain.models import MCPServerConfig, ToolDefinition


@dataclass(frozen=True)
class MCPToolRef:
    """Maps a discovered tool back to its MCP server session."""

    server_name: str
    tool_name: str
    session: ClientSession


@dataclass
class MCPSessionManager:
    """Manages MCP server connections for a single call.

    Created at pipeline start, cleaned up at call end.
    """

    call_id: UUID
    project_id: UUID
    _sessions: dict[str, ClientSession] = field(default_factory=dict)
    _tool_refs: dict[str, MCPToolRef] = field(default_factory=dict)
    _exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    _connected: bool = False

    async def connect_servers(
        self,
        servers: list[MCPServerConfig],
    ) -> list[ToolDefinition]:
        """Connect to all configured MCP servers and discover tools.

        Returns the discovered tools as TurnCall ToolDefinition objects.
        """
        import asyncio

        from turncall.config.settings import get_settings

        settings = get_settings()

        # Phase 1 — open transports SERIALLY. Entering a transport/session
        # context runs inside an anyio cancel scope that must be exited in the
        # same task it was entered in; the shared _exit_stack is closed from the
        # call-cleanup task, so all entries must happen here (not in gather
        # children) to keep enter/exit in one task.
        opened: list[tuple[MCPServerConfig, ClientSession]] = []
        for server in servers:
            try:
                session = await self._create_session(server, settings)
                self._sessions[server.name] = session
                opened.append((server, session))
            except Exception:
                logger.exception(
                    "mcp_server_connect_failed",
                    server=server.name,
                    call_id=str(self.call_id),
                )

        # Phase 2 — initialize + discover CONCURRENTLY. These are pure JSON-RPC
        # round-trips on already-open sessions (no context entry/exit), so
        # running them in gather children carries no cancel-scope hazard and
        # collapses the per-server handshake latency into one round.
        async def _discover(
            server: MCPServerConfig, session: ClientSession
        ) -> list[ToolDefinition]:
            try:
                tools = await self._discover_tools(server, session, settings)
                logger.info(
                    "mcp_server_connected",
                    server=server.name,
                    transport=server.transport,
                    tools=len(tools),
                    call_id=str(self.call_id),
                )
                return tools
            except Exception:
                logger.exception(
                    "mcp_server_discover_failed",
                    server=server.name,
                    call_id=str(self.call_id),
                )
                return []

        results = await asyncio.gather(
            *(_discover(server, session) for server, session in opened)
        )

        self._connected = True
        all_tools: list[ToolDefinition] = []
        for tools in results:
            all_tools.extend(tools)
        return all_tools

    async def _discover_tools(
        self,
        server: MCPServerConfig,
        session: ClientSession,
        settings: Any,
    ) -> list[ToolDefinition]:
        """Handshake + tool discovery on an already-open session (Phase 2)."""
        # Initialize the MCP protocol
        await session.initialize()

        # Discover tools
        result = await session.list_tools()
        mcp_tools = result.tools

        # Apply tool filter
        if server.tool_filter:
            allowed = set(server.tool_filter)
            mcp_tools = [t for t in mcp_tools if t.name in allowed]

        # Apply max tools limit
        max_tools = settings.mcp.max_tools_per_server
        if len(mcp_tools) > max_tools:
            logger.warning(
                "mcp_tools_truncated",
                server=server.name,
                total=len(mcp_tools),
                max=max_tools,
            )
            mcp_tools = mcp_tools[:max_tools]

        # Convert to TurnCall ToolDefinition + register refs
        tools: list[ToolDefinition] = []
        for mcp_tool in mcp_tools:
            tool_def = _mcp_tool_to_definition(mcp_tool, server.name)
            self._tool_refs[tool_def.name] = MCPToolRef(
                server_name=server.name,
                tool_name=mcp_tool.name,
                session=session,
            )
            tools.append(tool_def)

        return tools

    async def _create_session(
        self,
        server: MCPServerConfig,
        settings: Any,
    ) -> ClientSession:
        """Create an MCP ClientSession for the given transport."""
        if server.transport == "stdio":
            return await self._create_stdio_session(server, settings)
        if server.transport == "sse":
            return await self._create_sse_session(server)
        return await self._create_http_session(server)

    async def _create_stdio_session(
        self,
        server: MCPServerConfig,
        settings: Any,
    ) -> ClientSession:
        """Create a stdio transport session (local subprocess)."""
        if not settings.mcp.stdio_enabled:
            msg = (
                "stdio MCP transport is disabled. "
                "Set MCP_STDIO_ENABLED=true to enable."
            )
            raise ValueError(msg)

        command = server.command or ""
        allowed = settings.mcp.stdio_allowed_commands
        if command not in allowed:
            msg = (
                f"Command '{command}' not in allowed list: {allowed}. "
                "Update MCP_STDIO_ALLOWED_COMMANDS to allow it."
            )
            raise ValueError(msg)

        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=command,
            args=server.args,
            env={**server.env} if server.env else None,
        )
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(params)
        )
        read_stream, write_stream = stdio_transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        return session

    async def _create_sse_session(self, server: MCPServerConfig) -> ClientSession:
        """Create an SSE transport session."""
        from mcp.client.sse import sse_client

        sse_transport = await self._exit_stack.enter_async_context(
            sse_client(
                url=server.url or "",
                headers={
                    **server.headers,
                    "X-Call-Id": str(self.call_id),
                    "X-Project-Id": str(self.project_id),
                },
                timeout=server.timeout_seconds,
            )
        )
        read_stream, write_stream = sse_transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        return session

    async def _create_http_session(self, server: MCPServerConfig) -> ClientSession:
        """Create a streamable HTTP transport session."""
        from mcp.client.streamable_http import streamablehttp_client

        http_transport = await self._exit_stack.enter_async_context(
            streamablehttp_client(
                url=server.url or "",
                headers={
                    **server.headers,
                    "X-Call-Id": str(self.call_id),
                    "X-Project-Id": str(self.project_id),
                },
                timeout=server.timeout_seconds,
            )
        )
        read_stream, write_stream, _ = http_transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        return session

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool by name. Returns the result as a string."""
        ref = self._tool_refs.get(tool_name)
        if ref is None:
            return json.dumps({"error": f"MCP tool not found: {tool_name}"})

        try:
            result: CallToolResult = await ref.session.call_tool(
                ref.tool_name, arguments
            )

            if result.isError:
                error_text = ""
                for block in result.content:
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({"error": error_text or "MCP tool error"})

            # Extract text content from result
            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts) if parts else "{}"

        except Exception as exc:
            logger.exception(
                "mcp_tool_call_error",
                tool=tool_name,
                server=ref.server_name,
                call_id=str(self.call_id),
            )
            return json.dumps({"error": str(exc)})

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return tool_name in self._tool_refs

    async def close(self) -> None:
        """Disconnect all MCP sessions and clean up resources."""
        try:
            await self._exit_stack.aclose()
        except Exception:
            logger.exception("mcp_cleanup_error", call_id=str(self.call_id))
        self._sessions.clear()
        self._tool_refs.clear()
        self._connected = False
        logger.info("mcp_sessions_closed", call_id=str(self.call_id))


def _mcp_tool_to_definition(tool: Tool, server_name: str) -> ToolDefinition:
    """Convert an MCP Tool to a TurnCall ToolDefinition."""
    input_schema = tool.inputSchema or {}
    return ToolDefinition(
        name=tool.name,
        description=tool.description or f"MCP tool from {server_name}",
        parameters_schema={
            "type": input_schema.get("type", "object"),
            "properties": input_schema.get("properties", {}),
            "required": input_schema.get("required", []),
        },
        webhook_url=None,
        is_builtin=False,
    )
