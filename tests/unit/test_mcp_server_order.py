"""Which MCP server owns a contested tool name must follow the agent's config,
not the network.

Discovery runs concurrently, which is right — three servers' handshakes should
cost one round trip, not three. But name claiming used to happen inside the
gather children, so "first server wins" really meant "fastest server wins":
the same config could resolve a collision differently from one call to the
next, and the total-tools cap could cut off a different server each time.
"""

import asyncio
import uuid

import pytest

from tests.conftest import mcp_tool
from turncall.domain.models import MCPServerConfig
from turncall.services.mcp_client import MCPSessionManager


def _servers(*names: str) -> list[MCPServerConfig]:
    return [
        MCPServerConfig(name=n, transport="http", url=f"https://{n}.example/mcp")
        for n in names
    ]


async def _connect(manager: MCPSessionManager, servers, fetched: dict) -> list:
    """Run connect_servers with transports stubbed and `fetched` driving what
    each server returns and how slowly."""

    async def fake_create(server, settings):
        return object()

    async def fake_discover(server, session):
        delay, tools = fetched[server.name]
        await asyncio.sleep(delay)
        return tools

    manager._create_session = fake_create  # type: ignore[method-assign]
    manager._discover_tools = fake_discover  # type: ignore[method-assign]
    return await manager.connect_servers(servers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_first_configured_server_claims_a_shared_name() -> None:
    """The slow server is first in config and must still win. Under the old
    code it lost, because the fast one registered while it was still waiting."""
    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())

    tools = await _connect(
        manager,
        _servers("crm", "docs"),
        {
            "crm": (0.05, [mcp_tool("search")]),  # configured first, answers last
            "docs": (0.0, [mcp_tool("search")]),  # answers immediately
        },
    )

    assert manager._tool_refs["search"].server_name == "crm"
    assert [t.name for t in tools] == ["search"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discovery_still_overlaps() -> None:
    """The ordering fix must not have serialised the handshakes — that was the
    point of running them concurrently."""
    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())
    active = peak = 0

    async def fake_create(server, settings):
        return object()

    async def fake_discover(server, session):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return []

    manager._create_session = fake_create  # type: ignore[method-assign]
    manager._discover_tools = fake_discover  # type: ignore[method-assign]
    await manager.connect_servers(_servers("a", "b", "c"))

    assert peak >= 2, f"handshakes no longer overlap (peak={peak})"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_come_back_in_configured_server_order() -> None:
    """The advertised list is built from this, and _build_tools_schema keeps
    the first of a duplicate — so its order has to be the config's."""
    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())

    tools = await _connect(
        manager,
        _servers("first", "second", "third"),
        {
            "first": (0.06, [mcp_tool("a")]),
            "second": (0.0, [mcp_tool("b")]),
            "third": (0.03, [mcp_tool("c")]),
        },
    )

    assert [t.name for t in tools] == ["a", "b", "c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_server_that_fails_discovery_does_not_shift_the_others() -> None:
    """One unreachable server must not hand its slot's precedence to whoever
    happened to be next."""
    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())

    async def fake_create(server, settings):
        return object()

    async def fake_discover(server, session):
        if server.name == "broken":
            raise RuntimeError("server is down")
        return [mcp_tool("search")]

    manager._create_session = fake_create  # type: ignore[method-assign]
    manager._discover_tools = fake_discover  # type: ignore[method-assign]
    tools = await manager.connect_servers(_servers("broken", "crm", "docs"))

    assert manager._tool_refs["search"].server_name == "crm"
    assert [t.name for t in tools] == ["search"]
