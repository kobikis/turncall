"""build_knowledge_preamble: prompt-mode full text + auto/tool awareness hint."""

from unittest.mock import AsyncMock
from uuid import uuid4

from turncall.services import retrieval
from turncall.services.retrieval import (
    KNOWLEDGE_AWARENESS_HINT,
    build_knowledge_preamble,
)


class _FakeSession:
    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return False


def _factory():
    return _FakeSession()


async def test_no_attachments_is_empty():
    assert await build_knowledge_preamble(_factory, []) == ""


async def test_auto_mode_adds_awareness_hint():
    out = await build_knowledge_preamble(
        _factory, [{"knowledge_base_id": str(uuid4()), "mode": "auto"}]
    )
    assert out == KNOWLEDGE_AWARENESS_HINT


async def test_tool_mode_adds_awareness_hint():
    out = await build_knowledge_preamble(
        _factory, [{"knowledge_base_id": str(uuid4()), "mode": "tool"}]
    )
    assert out == KNOWLEDGE_AWARENESS_HINT


async def test_prompt_mode_injects_full_text(monkeypatch):
    monkeypatch.setattr(retrieval, "get_full_text_context", AsyncMock(return_value="MENU TEXT"))
    out = await build_knowledge_preamble(
        _factory, [{"knowledge_base_id": str(uuid4()), "mode": "prompt"}]
    )
    assert out == "MENU TEXT"
    # prompt mode injects content — no need for the "you have a KB" hint
    assert KNOWLEDGE_AWARENESS_HINT not in out


async def test_prompt_and_auto_combined(monkeypatch):
    monkeypatch.setattr(retrieval, "get_full_text_context", AsyncMock(return_value="MENU"))
    out = await build_knowledge_preamble(
        _factory,
        [
            {"knowledge_base_id": str(uuid4()), "mode": "prompt"},
            {"knowledge_base_id": str(uuid4()), "mode": "auto"},
        ],
    )
    assert "MENU" in out and KNOWLEDGE_AWARENESS_HINT in out
