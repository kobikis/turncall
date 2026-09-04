"""Review finding #6: the tool result must reach the LLM before the invocation
logging + webhook dispatch — those run in the background, off the audio path."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_callback_fires_before_background_logging() -> None:
    call_context = SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=None,
        session_factory=AsyncMock(),
    )

    captured = {}

    class _LLM:
        def register_function(self, name, fn):
            captured["handler"] = fn

    tool = ToolDefinition(
        name="book",
        description="b",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="https://x/t",
    )

    order: list[str] = []
    release = asyncio.Event()

    async def slow_log(*a, **k):
        order.append("log_start")
        await release.wait()  # hold the background task open
        order.append("log_done")

    async def result_callback(result):
        order.append("callback")

    with (
        patch.object(tool_bridge, "_execute_webhook_tool", new=AsyncMock(return_value='{"ok":1}')),
        patch.object(tool_bridge, "_log_tool_result", new=slow_log),
    ):
        tool_bridge.register_tools(_LLM(), [tool], call_context)
        params = SimpleNamespace(
            function_name="book",
            arguments={"date": "tomorrow"},
            result_callback=result_callback,
        )
        await captured["handler"](params)
        await asyncio.sleep(0)  # let the spawned task start

    # The callback ran and returned; logging is still blocked in the background.
    assert order == ["callback", "log_start"], order
    release.set()
    await asyncio.sleep(0)
    assert "log_done" in order  # background task still completes
