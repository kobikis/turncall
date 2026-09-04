"""Webhook tools sign their POST when the tool has a webhook_secret."""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from turncall.domain.models import ToolDefinition
from turncall.events.webhook_signing import verify_signature
from turncall.orchestrator.tool_bridge import _execute_webhook_tool


@dataclass
class _FakeCallContext:
    call_id: uuid.UUID = field(default_factory=uuid.uuid4)
    project_id: uuid.UUID = field(default_factory=uuid.uuid4)


def _tool(**overrides: Any) -> ToolDefinition:
    return ToolDefinition(
        name="book_appointment",
        description="Book it",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://backend:8000/tools/book_appointment",
        **overrides,
    )


async def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    async def fake_post(self, url, *, content=None, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers or {}
        return httpx.Response(
            200, text='{"ok": true}', request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_when_secret_set(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_request(monkeypatch)
    secret = "s" * 32
    result = await _execute_webhook_tool(
        _tool(webhook_secret=secret), {"date": "tomorrow"}, _FakeCallContext()
    )

    assert result == '{"ok": true}'
    sig = captured["headers"]["X-TurnCall-Signature"]
    ts = int(captured["headers"]["X-TurnCall-Timestamp"])
    assert verify_signature(captured["content"], secret, sig, ts)
    assert json.loads(captured["content"])["tool_name"] == "book_appointment"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsigned_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_request(monkeypatch)
    await _execute_webhook_tool(_tool(), {}, _FakeCallContext())

    assert "X-TurnCall-Signature" not in captured["headers"]
    assert captured["headers"]["Content-Type"] == "application/json"
