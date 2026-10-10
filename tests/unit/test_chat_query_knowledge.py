"""`query_knowledge` on text turns (SMS, Chat API, WhatsApp text).

A knowledge base linked in `tool` mode gives a voice call a `query_knowledge`
tool the model calls when it decides to. The text path skipped tool mode
outright, so a document read that way was invisible to every chat, SMS and
WhatsApp text — and to the console's Test Chat, while a voice call read it.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.services.chat_tools import build_chat_tools


def _tool_link(description: str | None = "Look up the menu", **over):
    return SimpleNamespace(
        knowledge_base_id=uuid4(),
        mode="tool",
        top_k=4,
        similarity_threshold=0.2,
        tool_description=description,
        **over,
    )


async def _build(config: AgentConfig, knowledge):
    return await build_chat_tools(
        config,
        session_id=uuid4(),
        project_id=uuid4(),
        knowledge=knowledge,
        openai_api_key="sk-test",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_tool_mode_knowledge_base_is_offered_as_query_knowledge():
    tools = await _build(AgentConfig(), [_tool_link()])

    [schema] = tools.schemas
    assert schema["name"] == "query_knowledge"
    assert schema["description"] == "Look up the menu"
    assert schema["parameters"]["required"] == ["query"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_one_there_is_no_query_knowledge():
    tools = await _build(AgentConfig(), [])
    assert tools.schemas == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_description_takes_the_voice_default():
    tools = await _build(AgentConfig(), [_tool_link(description=None)])
    assert tools.schemas[0]["description"].startswith("Search the knowledge base")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_agents_own_tool_keeps_the_name():
    """As on a voice call: query_knowledge is added last, so a webhook tool
    the agent named the same stays theirs."""
    own = ToolDefinition(
        name="query_knowledge",
        description="Our own lookup",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://crm.test/hook",
    )
    tools = await _build(AgentConfig(tools=[own]), [_tool_link()])
    assert [s["description"] for s in tools.schemas] == ["Our own lookup"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calling_it_searches_every_tool_mode_knowledge_base():
    first, second = _tool_link(), _tool_link()
    tools = await _build(AgentConfig(), [first, second])
    found = {"results": "[Knowledge Base Context]...", "count": 2}

    with (
        patch(
            "turncall.services.chat_tools.query_knowledge",
            new=AsyncMock(return_value=found),
        ) as search,
        patch("turncall.storage.database.get_session_factory") as factory,
        patch("turncall.services.chat_tools._record_invocation", new=AsyncMock()),
    ):
        factory.return_value = lambda: _Session()
        result = await tools.execute("query_knowledge", {"query": "salmon roll"})

    assert json.loads(result) == found
    kwargs = search.await_args.kwargs
    assert kwargs["query"] == "salmon roll"
    assert kwargs["knowledge_base_ids"] == [
        first.knowledge_base_id,
        second.knowledge_base_id,
    ]
    # The first link's settings, as the voice tool takes them.
    assert (kwargs["top_k"], kwargs["similarity_threshold"]) == (4, 0.2)
    assert kwargs["openai_api_key"] == "sk-test"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_search_is_an_error_the_model_reads_not_a_raise():
    tools = await _build(AgentConfig(), [_tool_link()])
    with (
        patch(
            "turncall.services.chat_tools.query_knowledge",
            new=AsyncMock(side_effect=RuntimeError("embeddings down")),
        ),
        patch("turncall.storage.database.get_session_factory") as factory,
        patch("turncall.services.chat_tools._record_invocation", new=AsyncMock()),
    ):
        factory.return_value = lambda: _Session()
        result = await tools.execute("query_knowledge", {"query": "hours"})

    assert "embeddings down" in json.loads(result)["error"]


class _Session:
    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *_):
        return False
