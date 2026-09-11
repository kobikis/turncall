"""Tool execution for text sessions (SMS, Chat API, WhatsApp text).

The voice path routes tools through tool_bridge, which needs a live call:
every built-in it dispatches takes a call_id. Text sessions have a session_id
and no call, so they get webhook + MCP tools and nothing else.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from turncall.domain.models import AgentConfig, MCPServerConfig, ToolDefinition
from turncall.services.chat_tools import build_chat_tools


def _webhook_tool(name: str = "book_meeting") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Book a meeting",
        parameters_schema={
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
        },
        webhook_url="http://crm.test/hook",
    )


def _builtin(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description="", parameters_schema={})


async def _build(config: AgentConfig, **kw):
    return await build_chat_tools(
        config, session_id=uuid4(), project_id=uuid4(), **kw
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_tools_configured_is_empty_and_closes_cleanly():
    tools = await _build(AgentConfig())
    assert tools.schemas == []
    await tools.aclose()  # must not blow up with nothing to close


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webhook_tool_becomes_an_openai_function_schema():
    tools = await _build(AgentConfig(tools=[_webhook_tool()]))

    assert tools.schemas == [
        {
            "name": "book_meeting",
            "description": "Book a meeting",
            "parameters": {
                "type": "object",
                "properties": {"day": {"type": "string"}},
                "required": ["day"],
            },
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_builtins_are_dropped():
    """end_call, transfer_call, send_dtmf and handoff_to_agent all resolve
    through call_control against a call_id. A text session has none, so
    offering them would give the model tools that can only fail."""
    config = AgentConfig(
        tools=[
            _builtin("end_call"),
            _builtin("transfer_call"),
            _builtin("send_dtmf"),
            _builtin("handoff_to_agent"),
            _webhook_tool(),
        ]
    )
    tools = await _build(config)

    assert [s["name"] for s in tools.schemas] == ["book_meeting"]
    assert "end_call" in await tools.execute("end_call", {})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_posts_the_session_envelope(monkeypatch):
    """call_id is the voice identifier and stays null here; session_id is what
    a text conversation actually has."""
    session_id, project_id = uuid4(), uuid4()
    captured: dict = {}

    async def fake_post(self, url, *, content=None, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["body"] = json.loads(content)
        return httpx.Response(200, text='{"ok": 1}', request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    tools = await build_chat_tools(
        AgentConfig(tools=[_webhook_tool()]),
        session_id=session_id,
        project_id=project_id,
    )
    result = await tools.execute("book_meeting", {"day": "friday"})

    assert result == '{"ok": 1}'
    assert captured["url"] == "http://crm.test/hook"
    assert captured["body"] == {
        "tool_name": "book_meeting",
        "arguments": {"day": "friday"},
        "project_id": str(project_id),
        "call_id": None,
        "session_id": str(session_id),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failing_webhook_comes_back_as_text(monkeypatch):
    """The loop relays whatever execute returns straight to the model, so a
    raise here would lose the customer's whole reply over one bad endpoint."""

    async def boom(self, url, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    tools = await _build(AgentConfig(tools=[_webhook_tool()]))
    result = await tools.execute("book_meeting", {"day": "friday"})

    assert "error" in json.loads(result)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_tool_name_is_reported_not_raised():
    tools = await _build(AgentConfig(tools=[_webhook_tool()]))
    assert "error" in json.loads(await tools.execute("nonexistent", {}))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_tools_are_discovered_and_routed():
    discovered = ToolDefinition(
        name="crm_lookup", description="Look up", parameters_schema={"type": "object"}
    )
    manager = SimpleNamespace(
        connect_servers=AsyncMock(return_value=[discovered]),
        call_tool=AsyncMock(return_value='{"found": true}'),
        is_mcp_tool=lambda name: name == "crm_lookup",
        close=AsyncMock(),
    )
    config = AgentConfig(
        tools=[_webhook_tool()],
        mcp_servers=[MCPServerConfig(name="crm", url="http://mcp.test/mcp")],
    )

    with patch("turncall.services.mcp_client.MCPSessionManager", return_value=manager):
        tools = await _build(config)
        assert {s["name"] for s in tools.schemas} == {"book_meeting", "crm_lookup"}
        assert await tools.execute("crm_lookup", {"id": 7}) == '{"found": true}'
        await tools.aclose()

    manager.call_tool.assert_awaited_once_with("crm_lookup", {"id": 7})
    manager.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_connect_failure_leaves_the_webhook_tools_working():
    """A text reply shouldn't die because an MCP server is down."""
    manager = SimpleNamespace(
        connect_servers=AsyncMock(side_effect=RuntimeError("refused")),
        close=AsyncMock(),
    )
    config = AgentConfig(
        tools=[_webhook_tool()],
        mcp_servers=[MCPServerConfig(name="crm", url="http://mcp.test/mcp")],
    )

    with patch("turncall.services.mcp_client.MCPSessionManager", return_value=manager):
        tools = await _build(config)
        await tools.aclose()

    assert [s["name"] for s in tools.schemas] == ["book_meeting"]
    manager.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_mcp_tool_cannot_shadow_a_configured_tool():
    """Matches the voice path: static tools are the customer's own and win a
    name clash, both in what's advertised and in what execute() routes to."""
    clashing = ToolDefinition(
        name="book_meeting", description="from mcp", parameters_schema={}
    )
    manager = SimpleNamespace(
        connect_servers=AsyncMock(return_value=[clashing]),
        call_tool=AsyncMock(return_value="mcp-ran"),
        is_mcp_tool=lambda name: name == "book_meeting",
        close=AsyncMock(),
    )
    config = AgentConfig(
        tools=[_webhook_tool()],
        mcp_servers=[MCPServerConfig(name="crm", url="http://mcp.test/mcp")],
    )

    with patch("turncall.services.mcp_client.MCPSessionManager", return_value=manager):
        tools = await _build(config)

    assert [s["name"] for s in tools.schemas] == ["book_meeting"]
    assert tools.schemas[0]["description"] == "Book a meeting"
    manager.call_tool.assert_not_awaited()
