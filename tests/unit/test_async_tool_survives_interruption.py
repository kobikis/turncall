"""execution_mode "async" must survive an actual interruption, not just set a flag.

test_async_tools.py covers the registration seam: that `async` maps to
`cancel_on_interruption=False`. That is the wiring, not the behaviour. This
drives Pipecat's own interruption handler over two registered tools and checks
which one is still running afterwards — the thing a caller talking over the
agent actually does.
"""

import asyncio

import pytest

from turncall.domain.enums import ToolExecutionMode
from turncall.domain.models import ToolDefinition
from turncall.orchestrator import tool_bridge


def _tool(name: str, mode: ToolExecutionMode) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="https://slow.example/tool",
        execution_mode=mode,
    )


class _LLM:
    """Just enough of LLMService to exercise its real interruption handler."""

    def __init__(self) -> None:
        self._functions: dict = {}
        self.cancelled: list[str] = []

    def register_function(self, name, handler, **options):
        from pipecat.services.llm_service import FunctionCallRegistryItem

        self._functions[name] = FunctionCallRegistryItem(
            function_name=name,
            handler=handler,
            cancel_on_interruption=options.get("cancel_on_interruption", True),
        )

    async def _cancel_function_call(self, name: str) -> None:
        self.cancelled.append(name)

    # The method under test, taken from the real class rather than reimplemented.
    async def _handle_interruptions(self, frame) -> None:
        from pipecat.services.llm_service import LLMService

        await LLMService._handle_interruptions(self, frame)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_the_sync_tool_is_cancelled_when_the_caller_barges_in() -> None:
    from pipecat.frames.frames import InterruptionFrame

    llm = _LLM()
    ctx = _context()
    tool_bridge.register_tools(
        llm,
        [
            _tool("quick_lookup", ToolExecutionMode.SYNC),
            _tool("slow_lookup", ToolExecutionMode.ASYNC),
        ],
        ctx,
    )

    await llm._handle_interruptions(InterruptionFrame())

    assert llm.cancelled == ["quick_lookup"], (
        "the async tool was cancelled by an interruption — execution_mode "
        f"'async' bought nothing. cancelled={llm.cancelled}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_async_tool_still_delivers_its_result_after_an_interruption() -> None:
    """The other half: not being cancelled is only useful if the result still
    reaches the model when it finally arrives."""
    from unittest.mock import AsyncMock, patch

    from pipecat.frames.frames import InterruptionFrame

    llm = _LLM()
    ctx = _context()
    release = asyncio.Event()
    delivered: list[str] = []

    async def slow_webhook(*_a, **_k):
        await release.wait()
        return '{"answer": 42}'

    async def result_callback(result):
        delivered.append(result)

    with (
        patch.object(tool_bridge, "_execute_webhook_tool", new=slow_webhook),
        patch.object(tool_bridge, "_log_tool_result", new=AsyncMock()),
    ):
        tool_bridge.register_tools(
            llm, [_tool("slow_lookup", ToolExecutionMode.ASYNC)], ctx
        )
        handler = llm._functions["slow_lookup"].handler
        from types import SimpleNamespace

        call = asyncio.create_task(
            handler(
                SimpleNamespace(
                    function_name="slow_lookup",
                    arguments={},
                    result_callback=result_callback,
                )
            )
        )
        await asyncio.sleep(0)

        # The caller talks over the agent while the lookup is still out.
        await llm._handle_interruptions(InterruptionFrame())
        assert not call.done(), "the in-flight async call was torn down"

        release.set()
        await asyncio.wait_for(call, timeout=2)

    assert delivered == ['{"answer": 42}']


def _context():
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_manager=None,
        session_factory=AsyncMock(),
        # A real call: no eval mocks intercepting the dispatch.
        tool_mocks=None,
        is_eval=False,
    )
