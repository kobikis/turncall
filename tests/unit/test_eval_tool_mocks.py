"""An eval's tool mocks short-circuit the bridge, and an unmocked call is
refused rather than executed (#71).

Pipecat's evals framework has no mocking: a `function_call` expectation
observes that a call happened, it does not intercept it. Without this seam a
scenario exercising a booking tool books a real appointment on every
iteration, and the agent's MCP servers are connected exactly as in production.

Written at the same seam as test_tool_dispatch_precedence: the real handler
registered by `register_tools`, invoked as pipecat invokes it.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge
from turncall.services.tool_mocks import ToolMocks


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

    def register_function(self, name, fn, **options):
        self.handlers[name] = fn


def _context(mocks: ToolMocks | None, mcp_manager: object | None = None):
    return SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=mcp_manager,
        session_factory=AsyncMock(),
        tool_mocks=mocks,
        is_eval=mocks is not None,
    )


def _webhook_tool(name: str = "book_appointment") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="books a real appointment",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="https://customer.example/book",
    )


def _mcp_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="discovered",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url=None,
    )


def _builtin_tool(name: str = "end_call") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="hangs up",
        parameters_schema={"type": "object", "properties": {}},
    )


async def _invoke(llm: _LLM, name: str, args: dict | None = None) -> str:
    out: dict[str, str] = {}

    async def result_callback(result: str) -> None:
        out["result"] = result

    await llm.handlers[name](
        SimpleNamespace(
            function_name=name,
            arguments=args or {},
            result_callback=result_callback,
        )
    )
    return out["result"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_mocked_webhook_tool_never_reaches_the_customers_endpoint() -> None:
    mocks = ToolMocks(responses={"book_appointment": {"status": "ok", "id": "APT-1"}})
    llm = _LLM()
    webhook = AsyncMock(return_value='{"from": "webhook"}')

    with patch.object(tool_bridge, "_execute_webhook_tool", new=webhook):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(mocks))
        result = await _invoke(llm, "book_appointment", {"when": "friday"})

    assert json.loads(result) == {"status": "ok", "id": "APT-1"}
    assert webhook.await_count == 0
    assert mocks.calls == [
        {
            "tool_name": "book_appointment",
            "arguments": {"when": "friday"},
            "result": result,
            "mocked": True,
        }
    ]
    assert mocks.refused == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_mocked_mcp_tool_contacts_no_server() -> None:
    manager = _MCPManager("search_crm")
    mocks = ToolMocks(responses={"search_crm": {"hits": []}})
    llm = _LLM()

    tool_bridge.register_tools(llm, [_mcp_tool("search_crm")], _context(mocks, manager))
    result = await _invoke(llm, "search_crm")

    assert json.loads(result) == {"hits": []}
    assert manager.called == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_mocked_builtin_does_not_touch_call_control() -> None:
    """An eval has no `calls` row, so a built-in is the one tool guaranteed to
    act on something that does not exist."""
    mocks = ToolMocks(responses={"end_call": {"success": True}})
    llm = _LLM()
    builtin = AsyncMock(return_value='{"from": "call_control"}')

    with patch.object(tool_bridge, "_execute_builtin", new=builtin):
        tool_bridge.register_tools(llm, [_builtin_tool()], _context(mocks))
        result = await _invoke(llm, "end_call")

    assert json.loads(result) == {"success": True}
    assert builtin.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unmocked_tool_is_refused_not_executed() -> None:
    """Fail closed: the default policy is what keeps a forgotten mock from
    becoming a real booking. The refusal names the tool so the run's error
    says which one."""
    mocks = ToolMocks(responses={"something_else": {}})
    llm = _LLM()
    webhook = AsyncMock(return_value='{"from": "webhook"}')

    with patch.object(tool_bridge, "_execute_webhook_tool", new=webhook):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(mocks))
        result = await _invoke(llm, "book_appointment")

    assert webhook.await_count == 0
    assert mocks.refused == ["book_appointment"]
    assert json.loads(result) == {"error": "unmocked tool: book_appointment"}
    assert mocks.calls[0]["mocked"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_executes_the_tool_and_records_it_as_real() -> None:
    mocks = ToolMocks(live=True)
    llm = _LLM()

    with (
        patch.object(
            tool_bridge,
            "_execute_webhook_tool",
            new=AsyncMock(return_value='{"from": "webhook"}'),
        ),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(mocks))
        result = await _invoke(llm, "book_appointment")

    assert result == '{"from": "webhook"}'
    assert mocks.refused == []
    assert mocks.calls == [
        {
            "tool_name": "book_appointment",
            "arguments": {},
            "result": '{"from": "webhook"}',
            "mocked": False,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_named_mock_still_wins_under_live() -> None:
    """`live` decides what happens to the tools no mock covers; a mock that was
    written is the explicit intent of the test either way."""
    mocks = ToolMocks(responses={"book_appointment": "already booked"}, live=True)
    llm = _LLM()
    webhook = AsyncMock(return_value='{"from": "webhook"}')

    with patch.object(tool_bridge, "_execute_webhook_tool", new=webhook):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(mocks))
        result = await _invoke(llm, "book_appointment")

    # A string mock is passed through as the tool result, not JSON-quoted.
    assert result == "already booked"
    assert webhook.await_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_real_call_has_no_mocks_and_dispatches_normally() -> None:
    """The seam must cost a live call nothing: `tool_mocks` is None there."""
    llm = _LLM()

    with (
        patch.object(
            tool_bridge,
            "_execute_webhook_tool",
            new=AsyncMock(return_value='{"from": "webhook"}'),
        ),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()) as log,
    ):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(None))
        result = await _invoke(llm, "book_appointment")

    assert result == '{"from": "webhook"}'
    # The invocation record is spawned off the critical path; let it run.
    await asyncio.sleep(0)
    assert log.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_oversized_mock_is_capped_like_any_other_tool_result() -> None:
    """A mock reaches the model by the route a webhook's answer does and sits
    in the context for the rest of the conversation. The boundary rejects one
    this big; rows stored before that check existed still have to be capped."""
    from turncall.services import tool_mocks as mocks_mod

    mocks = ToolMocks(responses={"lookup": "x" * 5000})
    llm = _LLM()
    small = SimpleNamespace(tools=SimpleNamespace(max_response_bytes=1000))

    with patch.object(mocks_mod, "get_settings", lambda: small):
        tool_bridge.register_tools(llm, [_webhook_tool("lookup")], _context(mocks))
        result = await _invoke(llm, "lookup")

    parsed = json.loads(result)
    assert "5000 bytes" in parsed["error"] and "limit 1000" in parsed["error"]
    assert parsed["preview"] == "x" * 512
    # What the model was actually handed, not what the scenario wrote.
    assert mocks.calls[0]["result"] == result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_eval_spawns_no_invocation_task() -> None:
    """An eval has no `calls` row, so the write is a foreign-key error every
    time. Decided before the task is created, not inside it."""
    mocks = ToolMocks(live=True)
    llm = _LLM()

    with (
        patch.object(
            tool_bridge,
            "_execute_webhook_tool",
            new=AsyncMock(return_value='{"from": "webhook"}'),
        ),
        patch.object(tool_bridge, "_spawn") as spawn,
    ):
        tool_bridge.register_tools(llm, [_webhook_tool()], _context(mocks))
        await _invoke(llm, "book_appointment")

    assert spawn.call_count == 0
