"""A static tool and an MCP tool can share a name. Whoever the schema named
has to be the one that runs.

_build_tools_schema gives the name to the agent's own tool (tests in
test_tool_advertising cover that). Dispatch used to ask the MCP manager by
name first, so the model saw the customer's description and schema and the
MCP server's tool ran — the two halves disagreed and only the advertising
half had a test.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge


class _MCPManager:
    def __init__(self, *names: str) -> None:
        self._names = set(names)
        self.called: list[str] = []

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._names

    async def call_tool(self, name: str, args: dict) -> str:
        self.called.append(name)
        return '{"from": "mcp"}'


class _LLM:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.registrations: list[str] = []

    def register_function(self, name, fn, **options):
        self.registrations.append(name)
        self.handlers[name] = fn


def _context(mcp_manager: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=mcp_manager,
        session_factory=AsyncMock(),
    )


def _webhook_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="the customer's own",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="https://customer.example/tool",
    )


def _mcp_tool(name: str) -> ToolDefinition:
    """What _mcp_tool_to_definition produces: no webhook_url."""
    return ToolDefinition(
        name=name,
        description="discovered",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url=None,
    )


async def _invoke(llm: _LLM, name: str) -> str:
    out: dict[str, str] = {}

    async def result_callback(result: str) -> None:
        out["result"] = result

    await llm.handlers[name](
        SimpleNamespace(
            function_name=name, arguments={}, result_callback=result_callback
        )
    )
    return out["result"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_configured_tool_runs_even_when_mcp_claims_the_name() -> None:
    manager = _MCPManager("lookup")
    llm = _LLM()

    with (
        patch.object(
            tool_bridge,
            "_execute_webhook_tool",
            new=AsyncMock(return_value='{"from": "webhook"}'),
        ),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        tool_bridge.register_tools(llm, [_webhook_tool("lookup")], _context(manager))
        result = await _invoke(llm, "lookup")

    assert result == '{"from": "webhook"}'
    assert manager.called == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_mcp_tool_still_routes_to_its_server() -> None:
    """The guard must not cost MCP tools their dispatch — they are exactly the
    ones with no webhook_url."""
    manager = _MCPManager("search")
    llm = _LLM()

    with patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()):
        tool_bridge.register_tools(llm, [_mcp_tool("search")], _context(manager))
        result = await _invoke(llm, "search")

    assert result == '{"from": "mcp"}'
    assert manager.called == ["search"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_is_deduplicated_first_wins() -> None:
    """Pipecat's registry is keyed by name. pipeline_builder passes
    config.tools + mcp_tools, so without dedup the discovered tool replaced
    the handler for a name the schema had given to the agent's own tool."""
    manager = _MCPManager("lookup")
    llm = _LLM()

    with (
        patch.object(
            tool_bridge,
            "_execute_webhook_tool",
            new=AsyncMock(return_value='{"from": "webhook"}'),
        ),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        tool_bridge.register_tools(
            llm,
            [_webhook_tool("lookup"), _mcp_tool("lookup")],
            _context(manager),
        )
        result = await _invoke(llm, "lookup")

    assert llm.registrations == ["lookup"]
    assert result == '{"from": "webhook"}'
    assert manager.called == []
