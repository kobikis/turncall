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
    # The awareness hint, and — tool mode only — search before saying no.
    assert out == retrieval.knowledge_hints({"tool"})
    assert out.startswith(KNOWLEDGE_AWARENESS_HINT)


async def test_prompt_mode_injects_full_text(monkeypatch):
    monkeypatch.setattr(
        retrieval, "get_full_text_context", AsyncMock(return_value="MENU TEXT")
    )
    out = await build_knowledge_preamble(
        _factory, [{"knowledge_base_id": str(uuid4()), "mode": "prompt"}]
    )
    assert out == "MENU TEXT"
    # prompt mode injects content — no need for the "you have a KB" hint
    assert KNOWLEDGE_AWARENESS_HINT not in out


async def test_prompt_and_auto_combined(monkeypatch):
    monkeypatch.setattr(
        retrieval, "get_full_text_context", AsyncMock(return_value="MENU")
    )
    out = await build_knowledge_preamble(
        _factory,
        [
            {"knowledge_base_id": str(uuid4()), "mode": "prompt"},
            {"knowledge_base_id": str(uuid4()), "mode": "auto"},
        ],
    )
    assert "MENU" in out and KNOWLEDGE_AWARENESS_HINT in out


def test_only_a_tool_mode_kb_adds_the_search_before_saying_no_rule():
    """In `tool` mode the model searches when it decides to, and one sure of
    the answer from the agent's name decides not to — an agent called Nonna
    denied serving sushi with a sushi menu one search away (live 2026-10-10)."""
    from turncall.services.retrieval import KNOWLEDGE_TOOL_HINT, knowledge_hints

    assert KNOWLEDGE_TOOL_HINT in knowledge_hints({"tool"})
    assert KNOWLEDGE_AWARENESS_HINT in knowledge_hints({"tool"})
    assert KNOWLEDGE_TOOL_HINT not in knowledge_hints({"auto"})
    assert knowledge_hints({"prompt"}) == ""


def test_no_hint_when_the_agents_own_tool_keeps_the_name():
    """An agent tool named `query_knowledge` keeps the name, so the search is
    never offered: telling the model to search with it would run the agent's
    tool instead, and a tool-mode KB is then one it cannot read at all."""
    from turncall.services.retrieval import knowledge_hints

    assert knowledge_hints({"tool"}, tool_names=["query_knowledge"]) == ""
    # An auto KB still retrieves on its own, so it is still worth saying.
    assert knowledge_hints(
        {"auto", "tool"}, tool_names=["query_knowledge"]
    ) == knowledge_hints({"auto"})


async def test_the_voice_preamble_skips_it_too():
    out = await build_knowledge_preamble(
        _factory,
        [{"knowledge_base_id": str(uuid4()), "mode": "tool"}],
        tool_names=["query_knowledge"],
    )
    assert out == ""
