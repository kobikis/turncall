"""C4 — MCP against a real external server, not our own fixture.

tests/integration covers all three transports against a server in this repo,
which shares our assumptions by construction. A stranger's server differs in the
ways that matter: schemas with `$defs` and `$ref`, its own error shapes, its own
notion of how much to return. This runs the reference implementation the MCP
project publishes — `@modelcontextprotocol/server-everything`, fetched from npm
at test time.

Needs `npx` on PATH. Skipped without it.
"""

import shutil
import uuid

import pytest
from tests.conftest import mcp_settings

from turncall.domain.models import MCPServerConfig
from turncall.services.mcp_client import MCPSessionManager

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_PACKAGE = "@modelcontextprotocol/server-everything"


@pytest.fixture
def npx() -> str:
    found = shutil.which("npx")
    if not found:
        pytest.skip("npx is not on PATH")
    return found


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, npx: str):
    stub = mcp_settings(stdio_enabled=True, stdio_allowed_commands=[npx])
    monkeypatch.setattr("turncall.config.settings.get_settings", lambda: stub)
    return stub


@pytest.fixture
async def manager(settings):
    mgr = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
    try:
        yield mgr
    finally:
        await mgr.close()


def _server(npx: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="everything",
        transport="stdio",
        command=npx,
        args=["-y", _PACKAGE],
        timeout_seconds=60,
    )


async def test_discovery_against_a_stranger_s_server(manager, npx: str) -> None:
    tools = await manager.connect_servers([_server(npx)])

    assert len(tools) > 5, f"expected the reference server's toolset, got {tools}"
    assert "echo" in {t.name for t in tools}


async def test_a_tool_call_round_trips(manager, npx: str) -> None:
    await manager.connect_servers([_server(npx)])

    result = await manager.call_tool("echo", {"message": "hello from turncall"})

    assert "hello from turncall" in result


async def test_every_discovered_schema_survives_conversion(manager, npx: str) -> None:
    """The B5 fix, against schemas nobody here wrote. Rebuilding them from
    type/properties/required used to drop `$defs`, leaving any `$ref` dangling —
    which providers reject outright."""
    tools = await manager.connect_servers([_server(npx)])

    for tool in tools:
        schema = tool.parameters_schema
        assert schema.get("type") == "object", tool.name
        refs = [
            value
            for prop in schema.get("properties", {}).values()
            if isinstance(prop, dict)
            for key, value in prop.items()
            if key == "$ref"
        ]
        if refs:
            assert "$defs" in schema or "definitions" in schema, (
                f"{tool.name} has a $ref with nowhere to resolve it: {refs}"
            )


async def test_an_unknown_tool_is_an_error_not_an_exception(manager, npx: str) -> None:
    """A model can hallucinate a name; that must come back as text it can read."""
    await manager.connect_servers([_server(npx)])

    result = await manager.call_tool("no_such_tool", {})

    assert "error" in result.lower()
