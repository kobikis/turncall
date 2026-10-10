"""A knowledge search on a call is recorded like any other tool call.

`query_knowledge` is registered straight on the LLM, outside `register_tools`,
so its calls were neither logged nor written to `tool_invocations`: a call
whose agent searched its menu read, from every record, as one that never
looked (seen on a live call, 2026-10-10 — found only in an LLM trace span).
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.services.llm_service import FunctionCallParams

from turncall.orchestrator import tool_bridge
from turncall.orchestrator.knowledge_processor import knowledge_tool


def _params(callback) -> FunctionCallParams:
    return FunctionCallParams(
        function_name="query_knowledge",
        tool_call_id="call_1",
        arguments={"query": "sushi"},
        llm=None,
        pipeline_worker=None,
        context=None,
        result_callback=callback,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_search_on_a_call_is_recorded(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(tool_bridge, "_spawn", lambda coro: spawned.append(coro))
    logged = AsyncMock()
    monkeypatch.setattr(tool_bridge, "_log_tool_result", logged)
    call = SimpleNamespace(call_id=uuid.uuid4(), is_eval=False)

    async def search(params):
        await params.result_callback({"results": "Salmon roll", "count": 1})

    delivered = AsyncMock()
    await tool_bridge.recorded_tool("query_knowledge", search, call)(_params(delivered))

    delivered.assert_awaited_once_with({"results": "Salmon roll", "count": 1})
    [coro] = spawned
    await coro
    _ctx, name, args, result, latency = logged.await_args.args
    assert (name, args) == ("query_knowledge", {"query": "sushi"})
    assert json.loads(result) == {"results": "Salmon roll", "count": 1}
    assert latency >= 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_eval_records_nothing(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(tool_bridge, "_spawn", lambda coro: spawned.append(coro))
    call = SimpleNamespace(call_id=uuid.uuid4(), is_eval=True)

    async def search(params):
        await params.result_callback({"count": 0})

    delivered = AsyncMock()
    await tool_bridge.recorded_tool("query_knowledge", search, call)(_params(delivered))

    delivered.assert_awaited_once()
    assert spawned == []  # no `calls` row behind an eval


@pytest.mark.unit
def test_the_calls_knowledge_tool_is_the_recorded_one():
    call = SimpleNamespace(call_id=uuid.uuid4(), is_eval=False)
    links = [{"knowledge_base_id": uuid.uuid4(), "mode": "tool"}]
    with patch.object(
        tool_bridge, "recorded_tool", wraps=tool_bridge.recorded_tool
    ) as recorded:
        tool = knowledge_tool(links, AsyncMock(), "sk", call)
    assert tool is not None
    recorded.assert_called_once()
    assert recorded.call_args.args[0] == "query_knowledge"
