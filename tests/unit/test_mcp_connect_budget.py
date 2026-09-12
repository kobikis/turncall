"""MCP discovery runs on a clock, because someone is listening to silence.

Every caller of connect_servers has a person waiting: Twilio, WebRTC and
WhatsApp voice have a caller mid-ring, and the text path has an SMS provider's
delivery timeout. Only the HTTP and SSE transports carry a timeout of their
own — stdio has none — so a server that accepts input and never answers held
the connect request open with no ceiling at all.

Losing the tools degrades the agent. Losing the call is an outage. The budget
picks the first.
"""

import asyncio
import uuid

import pytest

from tests.conftest import mcp_settings
from turncall.domain.models import MCPServerConfig
from turncall.services.mcp_client import MCPSessionManager


def _server(name: str = "slow") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="http", url=f"https://{name}.example/mcp"
    )


def _manager() -> MCPSessionManager:
    return MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_server_that_never_answers_gives_up_and_returns_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()

    async def never(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr(manager, "_create_session", never)
    monkeypatch.setattr(
        "turncall.config.settings.get_settings",
        lambda: mcp_settings(connect_timeout_seconds=0.05),
    )

    started = asyncio.get_running_loop().time()
    tools = await manager.connect_servers([_server()])
    elapsed = asyncio.get_running_loop().time() - started

    assert tools == []
    assert elapsed < 5, f"gave up after {elapsed:.2f}s — the budget did nothing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_budget_does_not_fire_on_a_server_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard against a fix that just breaks discovery."""
    from tests.conftest import mcp_tool

    manager = _manager()

    async def fast_create(*_a, **_k):
        return object()

    async def fast_discover(server, session):
        return [mcp_tool("search")]

    monkeypatch.setattr(manager, "_create_session", fast_create)
    monkeypatch.setattr(manager, "_discover_tools", fast_discover)
    monkeypatch.setattr(
        "turncall.config.settings.get_settings",
        lambda: mcp_settings(connect_timeout_seconds=5.0),
    )

    tools = await manager.connect_servers([_server("quick")])

    assert [t.name for t in tools] == ["search"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_transports_stay_closeable_after_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timing out cancels discovery mid-flight. Whatever it opened is still on
    the exit stack, and close() runs in this same task — which is what the
    anyio cancel scopes require."""
    manager = _manager()
    opened = asyncio.Event()

    async def open_then_hang(*_a, **_k):
        opened.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(manager, "_create_session", open_then_hang)
    monkeypatch.setattr(
        "turncall.config.settings.get_settings",
        lambda: mcp_settings(connect_timeout_seconds=0.05),
    )

    await manager.connect_servers([_server()])
    assert opened.is_set(), "the test never reached the transport"

    await manager.close()  # must not raise
    assert manager._tool_refs == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discovery_runs_in_the_calling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.timeout, not wait_for. The transports open anyio cancel scopes
    that must be exited by the task that entered them, so the budget must not
    move the body onto a task of its own."""
    manager = _manager()
    caller = asyncio.current_task()
    seen: list[object] = []

    async def record(*_a, **_k):
        seen.append(asyncio.current_task())
        raise RuntimeError("stop here")

    monkeypatch.setattr(manager, "_create_session", record)
    monkeypatch.setattr(
        "turncall.config.settings.get_settings",
        lambda: mcp_settings(connect_timeout_seconds=5.0),
    )

    await manager.connect_servers([_server()])

    assert seen == [caller], "discovery ran on a different task than its caller"
