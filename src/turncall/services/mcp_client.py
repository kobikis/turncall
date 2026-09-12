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

from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    MCPServerConfig,
    ToolDefinition,
)


@dataclass(frozen=True)
class MCPToolRef:
    """Maps a discovered tool back to its MCP server session."""

    server_name: str
    tool_name: str
    session: Any


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

        # Phase 2 — handshake + list_tools CONCURRENTLY. These are pure
        # JSON-RPC round-trips on already-open sessions (no context entry/exit),
        # so running them in gather children carries no cancel-scope hazard and
        # collapses the per-server handshake latency into one round. They only
        # *fetch*; nothing is claimed here.
        async def _fetch(server: MCPServerConfig, session: ClientSession) -> list[Tool]:
            try:
                return await self._discover_tools(server, session)
            except Exception:
                logger.exception(
                    "mcp_server_discover_failed",
                    server=server.name,
                    call_id=str(self.call_id),
                )
                return []

        fetched = await asyncio.gather(
            *(_fetch(server, session) for server, session in opened)
        )

        # Phase 3 — claim names SERIALLY, in the agent's configured order.
        # gather returns results in the order they were passed, not the order
        # they completed, so zipping them back onto `opened` restores it.
        # Claiming inside the gather children made "first server wins" mean
        # "fastest server wins": which server owned a shared name, and which
        # one the total-tools cap cut off, could differ from one call to the
        # next with no config change. Registration is synchronous, so this
        # loop stays a single uninterrupted pass.
        self._connected = True
        all_tools: list[ToolDefinition] = []
        for (server, session), mcp_tools in zip(opened, fetched, strict=True):
            tools = self._register_discovered(
                mcp_tools,
                server_name=server.name,
                session=session,
                settings=settings,
            )
            logger.info(
                "mcp_server_connected",
                server=server.name,
                transport=server.transport,
                tools=len(tools),
                call_id=str(self.call_id),
            )
            all_tools.extend(tools)
        return all_tools

    async def _discover_tools(
        self,
        server: MCPServerConfig,
        session: ClientSession,
    ) -> list[Tool]:
        """Handshake + list_tools on an already-open session (Phase 2).

        Returns what the server advertises, filtered by `tool_filter`. Naming
        them is Phase 3's job — see connect_servers.
        """
        await session.initialize()
        result = await session.list_tools()
        mcp_tools: list[Tool] = result.tools

        if server.tool_filter:
            allowed = set(server.tool_filter)
            mcp_tools = [t for t in mcp_tools if t.name in allowed]

        return mcp_tools

    def _register_discovered(
        self,
        mcp_tools: list[Tool],
        *,
        server_name: str,
        session: Any,
        settings: Any,
    ) -> list[ToolDefinition]:
        """Convert discovered tools and claim their names.

        The ref map is keyed by bare tool name, so a second server exposing a
        name the first already claimed would overwrite the route while both
        stayed advertised. First server wins; the loser is skipped and logged
        rather than silently shadowing.

        Built-in names are refused outright: tool dispatch checks those first,
        so an MCP tool called `end_call` would be advertised to the model and
        then hang up the call instead of running.
        """
        max_tools = settings.mcp.max_tools_per_server
        if len(mcp_tools) > max_tools:
            logger.warning(
                "mcp_tools_truncated",
                server=server_name,
                total=len(mcp_tools),
                max=max_tools,
            )
            mcp_tools = mcp_tools[:max_tools]

        max_total = getattr(settings.mcp, "max_tools_total", 0)

        tools: list[ToolDefinition] = []
        for mcp_tool in mcp_tools:
            if max_total and len(self._tool_refs) >= max_total:
                logger.warning(
                    "mcp_tools_total_cap_reached",
                    server=server_name,
                    max=max_total,
                )
                break

            tool_def = _mcp_tool_to_definition(mcp_tool, server_name)
            if tool_def.name in BUILTIN_TOOL_NAMES:
                logger.warning(
                    "mcp_tool_shadows_builtin",
                    tool=tool_def.name,
                    server=server_name,
                )
                continue

            claimed = self._tool_refs.get(tool_def.name)
            if claimed is not None:
                logger.warning(
                    "mcp_tool_name_collision",
                    tool=tool_def.name,
                    server=server_name,
                    claimed_by=claimed.server_name,
                )
                continue
            self._tool_refs[tool_def.name] = MCPToolRef(
                server_name=server_name,
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

        # An MCP url is an outbound target picked by whoever can write the
        # agent config, reached from inside the network — the same SSRF
        # surface the custom-LLM and S2S gateway endpoints are gated on.
        from turncall.services.url_allowlist import check_url_allowed

        check_url_allowed(
            server.url or "",
            settings.byom.allowed_url_patterns,
            label=f"MCP server '{server.name}' url",
        )

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
                "stdio MCP transport is disabled. Set MCP_STDIO_ENABLED=true to enable."
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
        """Create a streamable HTTP transport session.

        `streamable_http_client` is the name both SDK lines share (mcp 1.24+
        and all of 2.x); the older `streamablehttp_client` is 1.x-only. It
        takes its HTTP settings as a prepared client rather than as `headers`
        and `timeout`, so the client is ours to build and ours to close.
        """
        import sys

        from mcp.client.streamable_http import streamable_http_client

        # The client class has to come from the httpx family the SDK itself
        # was built against — httpx on the 1.x line, httpx2 on 2.x — so take
        # it from the transport's own module rather than importing one.
        transport_module = sys.modules[streamable_http_client.__module__]
        http = getattr(transport_module, "httpx2", None) or transport_module.httpx

        client = await self._exit_stack.enter_async_context(
            http.AsyncClient(
                headers=self._transport_headers(server),
                timeout=http.Timeout(server.timeout_seconds),
                # Matches the client the SDK builds when given none.
                follow_redirects=True,
            )
        )
        streams = await self._exit_stack.enter_async_context(
            streamable_http_client(server.url or "", http_client=client)
        )
        # 1.x yields (read, write, get_session_id); 2.x yields (read, write).
        session = await self._exit_stack.enter_async_context(
            ClientSession(streams[0], streams[1])
        )
        return session

    def _transport_headers(self, server: MCPServerConfig) -> dict[str, str]:
        """The customer's headers plus the call/project the request belongs to."""
        return {
            **server.headers,
            "X-Call-Id": str(self.call_id),
            "X-Project-Id": str(self.project_id),
        }

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool by name. Returns the result as a string."""
        ref = self._tool_refs.get(tool_name)
        if ref is None:
            return json.dumps({"error": f"MCP tool not found: {tool_name}"})

        try:
            result: CallToolResult = await ref.session.call_tool(
                ref.tool_name, arguments
            )

            if _result_is_error(result):
                error_text = ""
                for block in result.content:
                    if hasattr(block, "text"):
                        error_text += block.text
                # Capped too: a failing tool can return just as much text as
                # a succeeding one, and it lands in the same context.
                return _cap_response(
                    json.dumps({"error": error_text or "MCP tool error"}),
                    tool_name,
                    ref.server_name,
                )

            # Extract text content from result
            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            text = "\n".join(parts) if parts else "{}"
            return _cap_response(text, tool_name, ref.server_name)

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


def _cap_response(text: str, tool_name: str, server_name: str) -> str:
    """Keep a runaway MCP result out of the LLM context, per
    MCP_MAX_RESPONSE_BYTES. Webhook tools get the same treatment under their
    own limit — the behaviour lives in tool_webhook so both stay identical."""
    from turncall.config.settings import get_settings
    from turncall.services.tool_webhook import cap_tool_result

    return cap_tool_result(
        text,
        get_settings().mcp.max_response_bytes,
        tool=tool_name,
        source=f"mcp:{server_name}",
    )


def _tool_input_schema(tool: Tool) -> dict[str, Any]:
    """The tool's JSON schema, whichever way the SDK spells the field.

    mcp 2.x renamed `inputSchema` to `input_schema`. Reading only the old name
    raised AttributeError per tool, which connect_servers logs and swallows —
    so every server quietly returned nothing at all.
    """
    for name in ("inputSchema", "input_schema"):
        schema = getattr(tool, name, None)
        if isinstance(schema, dict):
            return schema
    return {}


def _result_is_error(result: CallToolResult) -> bool:
    """Whether the call failed — `isError` on mcp 1.x, `is_error` on 2.x."""
    for name in ("isError", "is_error"):
        flag = getattr(result, name, None)
        if isinstance(flag, bool):
            return flag
    return False


def _mcp_tool_to_definition(tool: Tool, server_name: str) -> ToolDefinition:
    """Convert an MCP Tool to a TurnCall ToolDefinition."""
    input_schema = _tool_input_schema(tool)
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
