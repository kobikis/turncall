"""A real MCP round trip over streamable HTTP.

Every other MCP test mocks the session, which is exactly how mcp 2.x shipped
broken: it renamed the two fields the client reads, and nothing in a mocked
suite touches a field name. This starts an actual server, connects the real
client to it, discovers a tool and calls it — so a transport or model change
in either SDK line fails here instead of in a call.

Runs against whichever line is installed; the client supports both.
"""

import socket
import threading
import time
from uuid import uuid4

import pytest

from turncall.domain.models import MCPServerConfig
from turncall.services.mcp_client import MCPSessionManager


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_server():
    """FastMCP on mcp 1.x, MCPServer on 2.x — the server side was renamed."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ModuleNotFoundError:
        from mcp.server.fastmcp import FastMCP as Server

    server = Server(name="probe")

    @server.tool()
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return f"sum={a + b}"

    return server


@pytest.fixture(scope="module")
def mcp_server_url() -> str:
    import uvicorn

    port = _free_port()
    server = _build_server()

    def serve() -> None:
        uvicorn.run(
            server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"
        )

    threading.Thread(target=serve, daemon=True).start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:  # pragma: no cover - only on a machine that can't bind a port
        pytest.skip("MCP probe server did not start")

    return f"http://127.0.0.1:{port}/mcp"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_discover_and_call_a_real_mcp_tool(mcp_server_url: str) -> None:
    manager = MCPSessionManager(call_id=uuid4(), project_id=uuid4())
    config = MCPServerConfig(
        name="probe",
        transport="http",
        url=mcp_server_url,
        headers={"X-Probe": "1"},
        timeout_seconds=15,
    )

    tools = await manager.connect_servers([config])
    assert [t.name for t in tools] == ["add"]
    # The schema survives whichever way the SDK spells the field.
    assert tools[0].parameters_schema["properties"].keys() >= {"a", "b"}

    assert manager.is_mcp_tool("add")
    assert not manager.is_mcp_tool("not_a_tool")

    result = await manager.call_tool("add", {"a": 2, "b": 3})
    assert "sum=5" in result

    await manager.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_tool_reports_rather_than_raises(mcp_server_url: str) -> None:
    manager = MCPSessionManager(call_id=uuid4(), project_id=uuid4())
    config = MCPServerConfig(
        name="probe", transport="http", url=mcp_server_url, timeout_seconds=15
    )
    await manager.connect_servers([config])

    assert "error" in await manager.call_tool("nonexistent", {})

    await manager.close()
