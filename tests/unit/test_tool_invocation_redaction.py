"""Tool-invocation rows are stored redacted (#314, ADR-0024).

A `tool_invocations` row outlives the call and is served by the invocations
reads, so for a project that redacts its arguments and result go through the
redaction first: as free JSON, every string analysed whatever the tool named
its key. The live call is untouched: the tool's webhook is sent the raw
arguments and the model is handed the raw result. And the wait on the
detector happens before the row's session is opened, as every redaction wait
does (events/emit).

On a stand-in detector, as `test_redaction.py`; the real one, through the
whole path, is `test_harness_redaction.py`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator import tool_bridge
from turncall.services import redaction
from turncall.services.chat_tools import build_chat_tools
from turncall.services.redaction import DEFAULT_ENTITIES, RedactionPolicy
from turncall.services.tool_webhook import invocation_record

pytestmark = pytest.mark.unit

SECRET = "jane.doe@example.com"
PHONE = "4155550132"
POLICY = RedactionPolicy(DEFAULT_ENTITIES)


class _Marks:
    """Replaces SECRET wherever it appears, and PHONE's digits standing
    alone; records what it saw, and the order of its runs against the
    sessions opened (`happened`)."""

    def __init__(self, happened: list[str]) -> None:
        self.seen: list[str] = []
        self.happened = happened
        self._lock = threading.Lock()

    def redact(self, text: str, entities: tuple[str, ...]) -> str:
        with self._lock:
            self.seen.append(text)
            self.happened.append("detect")
        if text == PHONE:
            return "<PHONE_NUMBER>"
        return text.replace(SECRET, "<EMAIL_ADDRESS>")


@pytest.fixture
def happened() -> list[str]:
    return []


@pytest.fixture
def detector(monkeypatch, happened) -> Iterator[_Marks]:
    marks = _Marks(happened)
    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", lambda: marks)
    monkeypatch.setattr(
        redaction, "_new_pool", lambda: ThreadPoolExecutor(max_workers=1)
    )
    monkeypatch.setattr(redaction, "ENGINE", redaction._Engine())
    yield marks
    redaction.ENGINE.shutdown()


@pytest.fixture
def redacting(monkeypatch) -> None:
    """Every project redacts, with the default entity types."""

    async def on(_project_id, _sessions=None):
        return POLICY

    monkeypatch.setattr(redaction, "_read_policy_alone", on)


class _Db:
    """A session that records when it is opened and committed."""

    def __init__(self, happened: list[str]) -> None:
        self.happened = happened

    async def __aenter__(self):
        self.happened.append("session")
        return self

    async def __aexit__(self, *exc):
        return False

    def begin_nested(self):
        return self

    async def get(self, *_):
        return None

    async def commit(self):
        self.happened.append("commit")


ARGS = {
    "customer_id": SECRET,
    "callback_url": f"mailto:{SECRET}",
    "slots": [{"note": f"email {SECRET}"}, 3],
}
REDACTED_ARGS = {
    "customer_id": "<EMAIL_ADDRESS>",
    "callback_url": "mailto:<EMAIL_ADDRESS>",
    "slots": [{"note": "email <EMAIL_ADDRESS>"}, 3],
}
RESULT = json.dumps({"booked": True, "confirmation": f"sent to {SECRET}"})
REDACTED_OUTPUT = {"booked": True, "confirmation": "sent to <EMAIL_ADDRESS>"}


async def test_a_redacting_projects_row_is_redacted_whatever_its_keys(
    detector, redacting
) -> None:
    """The keys are the tool's: `customer_id` and `callback_url` are what the
    caller said here, not TurnCall's identifiers."""
    args = json.loads(json.dumps(ARGS))

    status, input_json, output_json = await invocation_record(uuid4(), args, RESULT)

    assert (status, input_json, output_json) == (
        "succeeded",
        REDACTED_ARGS,
        REDACTED_OUTPUT,
    )
    assert args == ARGS  # the caller's arguments are left as they were


async def test_the_status_is_read_from_the_raw_result(detector, redacting) -> None:
    status, _, output_json = await invocation_record(
        uuid4(), {}, json.dumps({"error": f"no customer {SECRET}"})
    )

    assert status == "failed"
    assert output_json == {"error": "no customer <EMAIL_ADDRESS>"}


async def test_a_project_with_redaction_off_stores_the_row_as_it_was(
    detector,
) -> None:
    status, input_json, output_json = await invocation_record(uuid4(), ARGS, RESULT)

    assert (status, input_json, output_json) == (
        "succeeded",
        ARGS,
        json.loads(RESULT),
    )
    assert detector.seen == []


async def test_a_voice_tool_row_is_redacted_before_its_session_opens(
    detector, redacting, happened
) -> None:
    create = AsyncMock()
    context = SimpleNamespace(
        project_id=uuid4(), call_id=uuid4(), session_factory=lambda: _Db(happened)
    )

    with (
        patch(
            "turncall.storage.repositories.tool_invocation_repo.create_invocation",
            new=create,
        ),
        patch("turncall.events.emit.emit_call_event", new=AsyncMock()),
    ):
        await tool_bridge._log_tool_result(context, "book", ARGS, RESULT, 12)

    kwargs = create.await_args.kwargs
    assert kwargs["call_id"] == context.call_id
    assert kwargs["input_json"] == REDACTED_ARGS
    assert kwargs["output_json"] == REDACTED_OUTPUT
    assert kwargs["status"] == "succeeded"
    # Every wait on the detector came before the session was opened.
    assert "detect" in happened
    assert "detect" not in happened[happened.index("session") :]


async def test_a_text_tool_row_is_redacted_but_the_webhook_and_model_get_it_raw(
    detector, redacting, happened, monkeypatch
) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_post(self, url, **kw):
        sent.append(json.loads(kw["content"]))
        return httpx.Response(200, text=RESULT, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    create = AsyncMock()
    tool = ToolDefinition(
        name="book",
        description="Book",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://crm.test/hook",
    )

    with (
        patch(
            "turncall.storage.database.get_session_factory",
            return_value=lambda: _Db(happened),
        ),
        patch(
            "turncall.storage.repositories.tool_invocation_repo.create_invocation",
            new=create,
        ),
        patch("turncall.events.dispatcher.dispatch_event", new=AsyncMock()),
    ):
        tools = await build_chat_tools(
            AgentConfig(tools=[tool]), session_id=uuid4(), project_id=uuid4()
        )
        result = await tools.execute("book", ARGS)
        for _ in range(100):
            if create.await_count:
                break
            await asyncio.sleep(0.01)

    assert [s["arguments"] for s in sent] == [ARGS]  # the webhook: raw
    assert result == RESULT  # the model: raw
    kwargs = create.await_args.kwargs
    assert kwargs["input_json"] == REDACTED_ARGS
    assert kwargs["output_json"] == REDACTED_OUTPUT
    assert "detect" not in happened[happened.index("session") :]


async def test_an_integer_phone_in_a_tools_arguments_is_redacted_in_tool_result(
    detector, redacting, happened
) -> None:
    """A tool whose schema types `phone` as an integer is called with
    `{"phone": 4155550132}`. The `tool.result` event, stored and sent to
    subscribers, carries `<PHONE_NUMBER>` as the invocation row does: the
    payload's long integers are analysed as free JSON's are (#336 review)."""
    args = {"phone": int(PHONE), "party_size": 4}
    stored = AsyncMock()
    dispatched = AsyncMock()
    context = SimpleNamespace(
        project_id=uuid4(), call_id=uuid4(), session_factory=lambda: _Db(happened)
    )

    with (
        patch(
            "turncall.storage.repositories.tool_invocation_repo.create_invocation",
            new=AsyncMock(),
        ) as create,
        patch(
            "turncall.storage.repositories.call_repo.get_next_sequence_number",
            new=AsyncMock(return_value=1),
        ),
        patch("turncall.storage.repositories.call_repo.create_call_event", new=stored),
        patch("turncall.events.dispatcher.dispatch_event", new=dispatched),
    ):
        await tool_bridge._log_tool_result(context, "book", args, RESULT, 12)

    redacted = {"phone": "<PHONE_NUMBER>", "party_size": 4}
    assert create.await_args.kwargs["input_json"] == redacted
    assert stored.await_args.kwargs["event_type"] == "tool.result"
    assert stored.await_args.kwargs["payload"]["arguments"] == redacted
    assert dispatched.await_args.kwargs["payload"]["arguments"] == redacted
    assert args == {"phone": int(PHONE), "party_size": 4}
