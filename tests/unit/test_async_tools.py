"""execution_mode: "async" — tools that keep running while the agent talks.

The field has been in the enum and accepted by the API schema since tools
existed, and did nothing anywhere. Pipecat has the behaviour natively: a
function registered with cancel_on_interruption=False is what it calls an
async tool — its result is delivered when it arrives rather than cancelled
when the caller interrupts.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from turncall.domain.enums import ToolExecutionMode
from turncall.domain.models import ToolDefinition
from turncall.orchestrator.tool_bridge import register_tools


def _tool(name: str = "check_stock", **over) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://x/hook",
        **over,
    )


def _ctx():
    return SimpleNamespace(
        call_id=uuid4(), project_id=uuid4(), agent_id=uuid4(),
        session_factory=MagicMock(), mcp_manager=None,
    )


def _registered(tools: list[ToolDefinition]) -> dict[str, dict]:
    """name -> the kwargs register_function was called with."""
    llm = MagicMock()
    register_tools(llm, tools, _ctx())
    return {c.args[0]: c.kwargs for c in llm.register_function.call_args_list}


@pytest.mark.unit
def test_a_sync_tool_is_cancelled_on_interruption():
    """The default, and what every tool did before this existed: the caller
    talking over the agent drops the in-flight call."""
    calls = _registered([_tool()])
    assert calls["check_stock"]["cancel_on_interruption"] is True


@pytest.mark.unit
def test_an_async_tool_survives_an_interruption():
    calls = _registered([_tool(execution_mode=ToolExecutionMode.ASYNC)])
    assert calls["check_stock"]["cancel_on_interruption"] is False


@pytest.mark.unit
def test_the_mode_is_per_tool_not_per_agent():
    """One slow lookup shouldn't make the fast tools uninterruptible."""
    calls = _registered(
        [
            _tool("slow_lookup", execution_mode=ToolExecutionMode.ASYNC),
            _tool("quick_check"),
        ]
    )
    assert calls["slow_lookup"]["cancel_on_interruption"] is False
    assert calls["quick_check"]["cancel_on_interruption"] is True


@pytest.mark.unit
def test_builtins_default_to_sync():
    """end_call and friends act on the call itself — letting one outlive an
    interruption would hang up after the caller changed their mind."""
    builtin = ToolDefinition(
        name="end_call", description="", parameters_schema={}, is_builtin=True
    )
    calls = _registered([builtin])
    assert calls["end_call"]["cancel_on_interruption"] is True
