"""Text-session tool calls are recorded like voice ones.

`tool_invocations.call_id` was NOT NULL with an FK to `calls`, so a chat
session — which has no call — could record nothing. Chat history stores only
the customer and assistant text, so a text tool call left no trace anywhere:
nothing to debug with, nothing to audit, and no `tool.result` for subscribers.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.services.chat_tools import build_chat_tools
from turncall.services.tool_webhook import classify_tool_result


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="book_meeting",
        description="Book",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://crm.test/hook",
    )


class _Db:
    def __init__(self):
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.committed = True


async def _run_tool(monkeypatch, body: str, status_code: int = 200):
    """Execute one text tool call and return what it tried to record."""

    async def fake_post(self, url, **kw):
        return httpx.Response(
            status_code, text=body, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    session_id, project_id = uuid4(), uuid4()
    db = _Db()
    create = AsyncMock()
    dispatch = AsyncMock()

    with (
        patch("turncall.storage.database.get_session_factory", return_value=lambda: db),
        patch(
            "turncall.storage.repositories.tool_invocation_repo.create_invocation",
            new=create,
        ),
        patch("turncall.events.dispatcher.dispatch_event", new=dispatch),
    ):
        tools = await build_chat_tools(
            AgentConfig(tools=[_tool()]),
            session_id=session_id,
            project_id=project_id,
        )
        result = await tools.execute("book_meeting", {"day": "friday"})
        # Recording runs off the reply's critical path.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    return SimpleNamespace(
        result=result,
        create=create,
        dispatch=dispatch,
        db=db,
        session_id=session_id,
        project_id=project_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_successful_call_is_recorded_against_the_session(monkeypatch):
    run = await _run_tool(monkeypatch, '{"booked": true}')

    kwargs = run.create.await_args.kwargs
    assert kwargs["session_id"] == run.session_id
    assert kwargs.get("call_id") is None
    assert kwargs["tool_name"] == "book_meeting"
    assert kwargs["input_json"] == {"day": "friday"}
    assert kwargs["status"] == "succeeded"
    assert kwargs["output_json"] == {"booked": True}
    assert kwargs["latency_ms"] >= 0
    assert run.db.committed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failing_call_is_recorded_as_failed(monkeypatch):
    """The row is worth more when the tool misbehaved than when it worked."""
    run = await _run_tool(monkeypatch, "boom", status_code=500)

    assert run.create.await_args.kwargs["status"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_result_reaches_subscribers_with_the_session_id(monkeypatch):
    """Voice dispatches tool.result; text dispatching nothing meant the same
    agent looked active on calls and silent on SMS."""
    run = await _run_tool(monkeypatch, '{"ok": 1}')

    kwargs = run.dispatch.await_args.kwargs
    assert kwargs["session_id"] == run.session_id
    assert kwargs["project_id"] == run.project_id
    assert kwargs["payload"]["tool_name"] == "book_meeting"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_recording_failure_never_costs_the_reply(monkeypatch):
    """The row is best-effort: losing the audit trail must not lose the SMS."""

    async def fake_post(self, url, **kw):
        return httpx.Response(200, text='{"ok": 1}', request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with patch(
        "turncall.storage.database.get_session_factory",
        side_effect=RuntimeError("db is down"),
    ):
        tools = await build_chat_tools(
            AgentConfig(tools=[_tool()]),
            session_id=uuid4(),
            project_id=uuid4(),
        )
        result = await tools.execute("book_meeting", {})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert result == '{"ok": 1}'


@pytest.mark.unit
class TestClassifyToolResult:
    def test_json_object_passes_through(self) -> None:
        assert classify_tool_result('{"a": 1}') == ("succeeded", {"a": 1})

    def test_an_error_key_marks_it_failed(self) -> None:
        status, out = classify_tool_result('{"error": "timed out"}')
        assert status == "failed"
        assert out == {"error": "timed out"}

    def test_plain_text_is_wrapped_not_dropped(self) -> None:
        """Endpoints are allowed to answer in prose; the row still records it."""
        assert classify_tool_result("all good") == ("succeeded", {"result": "all good"})

    def test_a_json_scalar_is_wrapped_too(self) -> None:
        assert classify_tool_result("42") == ("succeeded", {"result": 42})


@pytest.mark.unit
def test_the_row_must_belong_to_exactly_one_owner() -> None:
    """The CHECK is what keeps call_id/session_id from both being null now
    that call_id is nullable — a caller that forgets fails at the write."""
    from turncall.storage.models import ToolInvocationRow

    checks = [
        str(c.sqltext)
        for c in ToolInvocationRow.__table__.constraints
        if hasattr(c, "sqltext")
    ]
    assert "num_nonnulls(call_id, session_id) = 1" in checks


@pytest.mark.unit
def test_there_is_exactly_one_alembic_head() -> None:
    """A migration nothing points at never runs, and two heads never merge."""
    from pathlib import Path

    versions = Path("alembic/versions")
    revisions, downs = set(), set()
    for f in versions.glob("*.py"):
        for line in f.read_text().splitlines():
            if line.startswith("revision"):
                revisions.add(line.split("=")[1].strip().strip("\"'"))
            elif line.startswith("down_revision"):
                downs.add(line.split("=")[1].strip().strip("\"'"))

    heads = revisions - downs
    assert len(heads) == 1, f"expected one head, got {heads}"
    # This slice's migration must still be in the chain something points at.
    assert "a9c1d3e5f7b2" in revisions
