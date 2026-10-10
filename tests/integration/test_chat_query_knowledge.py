"""A `tool`-mode knowledge base reaches a text turn as `query_knowledge`.

Through the running app: a knowledge base linked to the agent in `tool` mode,
then a Chat API message. The text LLM is stubbed, and what it was offered is
the assertion — the text path used to skip tool mode, so the model was never
told the tool existed.
"""

import pytest

from tests.harness import HARNESS_AGENT, run_app
from tests.harness.second_instance import STUB_TEXT_REPLY
from tests.integration.limits_support import SCRIPT
from turncall.services.llm_text import CompletionResult
from turncall.services.retrieval import KNOWLEDGE_AWARENESS_HINT, KNOWLEDGE_TOOL_HINT

pytestmark = pytest.mark.integration


async def test_chat_offers_query_knowledge_for_a_tool_mode_knowledge_base(monkeypatch):
    offered: list[dict] = []

    async def _complete(_config, messages, **kwargs) -> CompletionResult:
        offered.append(
            {"tools": kwargs.get("tools") or [], "prompt": messages[0]["content"]}
        )
        return CompletionResult(text=STUB_TEXT_REPLY, total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        kb = await app.api.post("/v1/knowledge-bases", json={"name": "menu"})
        assert kb.status_code == 201, kb.text
        linked = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={
                "knowledge_base_id": kb.json()["data"]["id"],
                "mode": "tool",
                "tool_description": "Look up dishes on the menu",
            },
        )
        assert linked.status_code == 201, linked.text
        r = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "do you have salmon?"}
        )

    assert r.status_code == 201, r.text
    [turn] = offered
    tool = next(t for t in turn["tools"] if t["name"] == "query_knowledge")
    assert tool["description"] == "Look up dishes on the menu"
    assert KNOWLEDGE_AWARENESS_HINT in turn["prompt"]
    assert KNOWLEDGE_TOOL_HINT in turn["prompt"]  # search before saying no
