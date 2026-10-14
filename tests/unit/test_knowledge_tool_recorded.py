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
from turncall.orchestrator.knowledge_processor import (
    create_knowledge_tool_handler,
    knowledge_tool,
)
from turncall.services.tool_mocks import ToolMocks


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
    call = SimpleNamespace(
        call_id=uuid.uuid4(),
        is_eval=False,
        records=True,
        has_database=True,
        tool_mocks=None,
    )

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
    call = SimpleNamespace(
        call_id=uuid.uuid4(),
        is_eval=True,
        records=False,
        has_database=True,
        tool_mocks=None,
    )

    async def search(params):
        await params.result_callback({"count": 0})

    delivered = AsyncMock()
    await tool_bridge.recorded_tool("query_knowledge", search, call)(_params(delivered))

    delivered.assert_awaited_once()
    assert spawned == []  # no `calls` row behind an eval


@pytest.mark.unit
def test_the_calls_knowledge_tool_is_the_recorded_one():
    call = SimpleNamespace(
        call_id=uuid.uuid4(),
        is_eval=False,
        records=True,
        has_database=True,
        tool_mocks=None,
    )
    links = [{"knowledge_base_id": uuid.uuid4(), "mode": "tool"}]
    with patch.object(
        tool_bridge, "recorded_tool", wraps=tool_bridge.recorded_tool
    ) as recorded:
        tool = knowledge_tool(links, AsyncMock(), "sk", call)
    assert tool is not None
    recorded.assert_called_once()
    assert recorded.call_args.args[0] == "query_knowledge"


def _eval_call(mocks: ToolMocks) -> SimpleNamespace:
    return SimpleNamespace(
        call_id=uuid.uuid4(),
        is_eval=True,
        records=False,
        has_database=True,
        tool_mocks=mocks,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_eval_mock_answers_and_nothing_is_searched():
    """Mocked like every other tool (#71): a scenario's mock is the answer,
    and the knowledge base is never queried."""
    mocks = ToolMocks(responses={"query_knowledge": {"results": "Mock menu"}})
    search = AsyncMock()
    delivered = AsyncMock()
    await tool_bridge.recorded_tool("query_knowledge", search, _eval_call(mocks))(
        _params(delivered)
    )

    search.assert_not_awaited()
    assert json.loads(delivered.await_args.args[0]) == {"results": "Mock menu"}
    [call] = mocks.calls
    assert (call["tool_name"], call["outcome"]) == ("query_knowledge", "mocked")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unmocked_search_is_refused_under_mock_only():
    mocks = ToolMocks()
    search = AsyncMock()
    delivered = AsyncMock()
    await tool_bridge.recorded_tool("query_knowledge", search, _eval_call(mocks))(
        _params(delivered)
    )

    search.assert_not_awaited()
    assert mocks.refused == ["query_knowledge"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_live_eval_searches_and_says_so():
    mocks = ToolMocks(live=True)

    async def search(params):
        await params.result_callback({"results": "Salmon roll", "count": 1})

    await tool_bridge.recorded_tool("query_knowledge", search, _eval_call(mocks))(
        _params(AsyncMock())
    )

    [call] = mocks.calls
    assert call["outcome"] == "executed"
    assert json.loads(call["result"]) == {"results": "Salmon roll", "count": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_voice_search_does_not_tell_the_model_why():
    """The exception's text is for the log: the model can say it aloud."""

    class _Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *_):
            return False

    handler = create_knowledge_tool_handler(
        knowledge_base_ids=[uuid.uuid4()], session_factory=_Sessions()
    )
    delivered = AsyncMock()
    with patch(
        "turncall.orchestrator.knowledge_processor.query_knowledge",
        new=AsyncMock(side_effect=RuntimeError("password=hunter2")),
    ):
        await handler(_params(delivered))

    delivered.assert_awaited_once_with({"error": "knowledge search failed"})
