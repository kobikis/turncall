"""A text turn's next round runs on what a tool changed (#430).

A `load_skill` in one round of the tool loop adds the skill's section to the
system prompt and its tools to the tools. `complete_text(refresh=)` asks for
both after every round of tool calls, and every dialect — OpenAI-compatible,
Anthropic, Bedrock Converse, Gemini — sends them on the next request, the
first system message replaced and any other kept. Without `refresh` the
requests are what they always were.
"""

import copy
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import AWSConfig, LLMConfig
from turncall.services.llm_text import TurnUpdate, complete_text

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_LOAD = {"name": "load_skill", "description": "Load a skill", "parameters": {}}
_SKILL_TOOL = {"name": "get_invoices", "description": "Invoices", "parameters": {}}
_MESSAGES = [
    {"role": "system", "content": "You are Maya."},
    {"role": "user", "content": "Why is my bill higher?"},
    {"role": "system", "content": "From history."},
]
_LOADED = TurnUpdate("You are Maya.\n\n## Skill: explain_bill", [_LOAD, _SKILL_TOOL])


def _recording_client(*payloads: dict[str, Any]) -> tuple[MagicMock, list[dict]]:
    """An HTTP client answering `payloads` in turn, keeping a copy of each
    request body as it was sent (the loop mutates one body across rounds)."""
    sent: list[dict] = []
    responses = iter(payloads)

    async def post(_url: str, *, json: dict, **_kw: Any) -> MagicMock:
        sent.append(copy.deepcopy(json))
        response = MagicMock()
        response.json.return_value = next(responses)
        return response

    client = AsyncMock()
    client.post.side_effect = post
    return client, sent


async def _run(config: LLMConfig, client: MagicMock, refresh: Any) -> str:
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            config,
            _MESSAGES,
            tools=[_LOAD],
            execute_tool=AsyncMock(return_value='{"success": true}'),
            refresh=refresh,
        )
    return out.text


class TestOpenAI:
    _wants: ClassVar[dict[str, Any]] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "load_skill",
                                "arguments": '{"name": "explain_bill"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 1},
    }
    _done: ClassVar[dict[str, Any]] = {
        "choices": [{"message": {"role": "assistant", "content": "Done."}}],
        "usage": {"total_tokens": 1},
    }

    async def test_the_next_round_has_the_new_prompt_and_tools(self) -> None:
        client, sent = _recording_client(self._wants, self._done)
        config = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="k")

        assert await _run(config, client, lambda: _LOADED) == "Done."

        first, second = sent
        assert [t["function"]["name"] for t in first["tools"]] == ["load_skill"]
        assert first["messages"][0]["content"] == "You are Maya."
        assert [t["function"]["name"] for t in second["tools"]] == [
            "load_skill",
            "get_invoices",
        ]
        assert second["messages"][0]["content"] == _LOADED.system_prompt
        assert second["messages"][2]["content"] == "From history."

    async def test_without_refresh_every_round_is_as_it_began(self) -> None:
        client, sent = _recording_client(self._wants, self._done)
        config = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="k")

        await _run(config, client, None)

        assert sent[1]["tools"] == sent[0]["tools"]
        assert sent[1]["messages"][0] == _MESSAGES[0]


class TestAnthropic:
    async def test_the_next_round_has_the_new_prompt_and_tools(self) -> None:
        client, sent = _recording_client(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "load_skill",
                        "input": {"name": "explain_bill"},
                    }
                ],
                "usage": {},
            },
            {"content": [{"type": "text", "text": "Done."}], "usage": {}},
        )
        config = LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="k")

        assert await _run(config, client, lambda: _LOADED) == "Done."

        first, second = sent
        assert first["system"][0]["text"] == "You are Maya.\n\nFrom history."
        assert [t["name"] for t in second["tools"]] == ["load_skill", "get_invoices"]
        assert second["system"] == [
            {
                "type": "text",
                "text": f"{_LOADED.system_prompt}\n\nFrom history.",
                "cache_control": {"type": "ephemeral"},
            }
        ]


class TestGoogle:
    async def test_the_next_round_has_the_new_prompt_and_tools(self) -> None:
        client, sent = _recording_client(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "load_skill",
                                        "args": {"name": "explain_bill"},
                                    }
                                }
                            ],
                        }
                    }
                ]
            },
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Ok"}]}}]},
        )
        config = LLMConfig(provider="google", api_key="g")

        assert await _run(config, client, lambda: _LOADED) == "Ok"

        second = sent[1]
        declared = second["tools"][0]["functionDeclarations"]
        assert [d["name"] for d in declared] == ["load_skill", "get_invoices"]
        assert second["systemInstruction"] == {
            "parts": [{"text": f"{_LOADED.system_prompt}\n\nFrom history."}]
        }


class TestBedrock:
    async def test_the_next_round_has_the_new_prompt_and_tools(self) -> None:
        sent: list[dict] = []
        replies = iter(
            [
                {
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "toolUse": {
                                        "toolUseId": "t1",
                                        "name": "load_skill",
                                        "input": {"name": "explain_bill"},
                                    }
                                }
                            ],
                        }
                    },
                    "usage": {},
                },
                {
                    "output": {
                        "message": {"role": "assistant", "content": [{"text": "Ok"}]}
                    },
                    "usage": {},
                },
            ]
        )

        def converse(**kwargs: Any) -> dict:
            sent.append(copy.deepcopy(kwargs))
            return next(replies)

        client = MagicMock()
        client.converse.side_effect = converse
        boto3 = MagicMock()
        boto3.client.return_value = client
        config = LLMConfig(provider="bedrock", model="amazon.nova-pro-v1:0")
        with patch.dict("sys.modules", {"boto3": boto3}):
            out = await complete_text(
                config,
                _MESSAGES,
                aws=AWSConfig(),
                tools=[_LOAD],
                execute_tool=AsyncMock(return_value="{}"),
                refresh=lambda: _LOADED,
            )

        assert out.text == "Ok"
        second = sent[1]
        assert [t["toolSpec"]["name"] for t in second["toolConfig"]["tools"]] == [
            "load_skill",
            "get_invoices",
        ]
        assert second["system"] == [
            {"text": f"{_LOADED.system_prompt}\n\nFrom history."}
        ]
