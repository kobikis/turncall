"""Tests for MCP server support."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.api.v1.schemas.agents import MCPServerSchema
from turncall.domain.models import MCPServerConfig, ToolDefinition
from turncall.services.mcp_client import MCPSessionManager, _mcp_tool_to_definition


@pytest.mark.unit
class TestMCPServerSchema:
    def test_valid_http_server(self) -> None:
        schema = MCPServerSchema(
            name="crm-tools",
            transport="http",
            url="https://mcp.example.com/mcp",
            headers={"Authorization": "Bearer test"},
        )
        assert schema.name == "crm-tools"
        assert schema.transport == "http"

    def test_valid_sse_server(self) -> None:
        schema = MCPServerSchema(
            name="sse-server",
            transport="sse",
            url="https://mcp.example.com/sse",
        )
        assert schema.transport == "sse"

    def test_valid_stdio_server(self) -> None:
        schema = MCPServerSchema(
            name="local-db",
            transport="stdio",
            command="python",
            args=["mcp_server.py"],
            env={"DB_URL": "postgres://..."},
        )
        assert schema.command == "python"
        assert schema.args == ["mcp_server.py"]

    def test_http_requires_url(self) -> None:
        with pytest.raises(ValueError, match="url is required"):
            MCPServerSchema(name="bad", transport="http")

    def test_sse_requires_url(self) -> None:
        with pytest.raises(ValueError, match="url is required"):
            MCPServerSchema(name="bad", transport="sse")

    def test_stdio_requires_command(self) -> None:
        with pytest.raises(ValueError, match="command is required"):
            MCPServerSchema(name="bad", transport="stdio")

    def test_url_must_be_http(self) -> None:
        with pytest.raises(ValueError, match="must start with http"):
            MCPServerSchema(name="bad", transport="http", url="ftp://bad.com")

    def test_invalid_transport(self) -> None:
        with pytest.raises(ValueError):
            MCPServerSchema(name="bad", transport="websocket", url="http://x.com")

    def test_name_pattern(self) -> None:
        with pytest.raises(ValueError):
            MCPServerSchema(name="Bad Name!", transport="http", url="http://x.com")

    def test_tool_filter(self) -> None:
        schema = MCPServerSchema(
            name="filtered",
            transport="http",
            url="https://mcp.example.com",
            tool_filter=["lookup", "create"],
        )
        assert schema.tool_filter == ["lookup", "create"]


@pytest.mark.unit
class TestMCPServerConfig:
    def test_frozen(self) -> None:
        config = MCPServerConfig(name="test", transport="http", url="https://x.com")
        with pytest.raises(Exception):
            config.name = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        config = MCPServerConfig(name="test")
        assert config.transport == "http"
        assert config.timeout_seconds == 10
        assert config.headers == {}
        assert config.tool_filter is None


@pytest.mark.unit
class TestMCPToolConversion:
    def test_basic_conversion(self) -> None:
        mcp_tool = MagicMock()
        mcp_tool.name = "lookup_customer"
        mcp_tool.description = "Look up a customer by phone"
        mcp_tool.inputSchema = {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Phone number"},
            },
            "required": ["phone"],
        }

        result = _mcp_tool_to_definition(mcp_tool, "crm-server")
        assert isinstance(result, ToolDefinition)
        assert result.name == "lookup_customer"
        assert result.description == "Look up a customer by phone"
        assert result.parameters_schema["properties"]["phone"]["type"] == "string"
        assert result.parameters_schema["required"] == ["phone"]
        assert result.webhook_url is None
        assert result.is_builtin is False

    def test_no_description(self) -> None:
        mcp_tool = MagicMock()
        mcp_tool.name = "my_tool"
        mcp_tool.description = None
        mcp_tool.inputSchema = {}

        result = _mcp_tool_to_definition(mcp_tool, "srv")
        assert "MCP tool from srv" in result.description

    def test_no_input_schema(self) -> None:
        mcp_tool = MagicMock()
        mcp_tool.name = "no_params"
        mcp_tool.description = "A tool"
        mcp_tool.inputSchema = None

        result = _mcp_tool_to_definition(mcp_tool, "srv")
        assert result.parameters_schema["type"] == "object"
        assert result.parameters_schema["properties"] == {}


@pytest.mark.unit
class TestMCPSessionManager:
    def test_is_mcp_tool_false_by_default(self) -> None:
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        assert mgr.is_mcp_tool("anything") is False

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self) -> None:
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        result = await mgr.call_tool("nonexistent", {})
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"]

    @pytest.mark.asyncio
    async def test_close_empty(self) -> None:
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        await mgr.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_stdio_disabled_by_default(self) -> None:
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        server = MCPServerConfig(
            name="local", transport="stdio", command="python", args=["server.py"]
        )

        with patch(
            "turncall.services.mcp_client.MCPSessionManager._create_session"
        ) as mock:
            mock.side_effect = ValueError("stdio MCP transport is disabled")
            tools = await mgr.connect_servers([server])
            # Should gracefully skip failed server
            assert tools == []

    @pytest.mark.asyncio
    async def test_connect_servers_graceful_failure(self) -> None:
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        server = MCPServerConfig(
            name="bad-server", transport="http", url="https://unreachable.example.com"
        )

        # A server that fails to open is skipped, not fatal.
        with patch.object(mgr, "_create_session", side_effect=ConnectionError("nope")):
            tools = await mgr.connect_servers([server])

        # Should return empty, not crash
        assert tools == []

    @pytest.mark.asyncio
    async def test_discovery_runs_concurrently(self) -> None:
        """Phase 2 (initialize + list_tools) fans out across servers — a slow
        server must not serialize the others."""
        import asyncio
        import uuid

        mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
        servers = [
            MCPServerConfig(name=f"s{i}", transport="http", url=f"https://h{i}")
            for i in range(3)
        ]

        # Sessions open serially (mocked, instant). Discovery sleeps — if it were
        # serial, 3x50ms; concurrent, ~50ms. Assert overlap.
        active = 0
        peak = 0

        async def fake_discover(server, session, settings):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return []

        with (
            patch.object(mgr, "_create_session", new=AsyncMock(return_value=object())),
            patch.object(mgr, "_discover_tools", side_effect=fake_discover),
        ):
            await mgr.connect_servers(servers)

        assert peak >= 2, f"discovery did not run concurrently (peak={peak})"


@pytest.mark.unit
class TestMCPSettings:
    def test_defaults(self) -> None:
        from turncall.config.settings import MCPSettings

        s = MCPSettings()
        assert s.stdio_enabled is False
        assert "python" in s.stdio_allowed_commands
        assert s.max_tools_per_server == 50
        assert s.max_response_bytes == 1_048_576
