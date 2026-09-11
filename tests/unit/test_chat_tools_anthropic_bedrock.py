"""Tool calling on the Anthropic and Bedrock text paths.

Both speak their own dialect: Anthropic uses `input_schema` and tool_use /
tool_result content blocks, Bedrock Converse wraps everything in toolSpec /
toolUse / toolResult. Until now complete_text logged a warning and dropped
the tools, so an SMS agent on Claude or Bedrock silently had none.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import AWSConfig, LLMConfig
from turncall.services.llm_text import _MAX_TOOL_ROUNDS, complete_text

_TOOLS = [
    {
        "name": "book_meeting",
        "description": "Book a meeting",
        "parameters": {
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
        },
    }
]
_MESSAGES = [{"role": "user", "content": "book friday"}]


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


def _anthropic_text(text: str, tokens: int = 5) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": tokens, "output_tokens": 0},
    }


def _anthropic_tool_use(tool_id: str, name: str, args: dict) -> dict:
    return {
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": tool_id, "name": name, "input": args},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


def _client(*payloads: dict) -> MagicMock:
    client = AsyncMock()
    responses = []
    for p in payloads:
        r = MagicMock()
        r.json.return_value = p
        responses.append(r)
    client.post.side_effect = responses
    return client


def _anthropic_config() -> LLMConfig:
    return LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="k")


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicTools:
    async def test_no_tools_sends_no_tools_key(self) -> None:
        client = _client(_anthropic_text("hi"))
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(_anthropic_config(), _MESSAGES)

        assert out.text == "hi"
        assert "tools" not in client.post.call_args.kwargs["json"]

    async def test_tools_use_input_schema_not_parameters(self) -> None:
        client = _client(_anthropic_text("hi"))
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            await complete_text(
                _anthropic_config(), _MESSAGES, tools=_TOOLS, execute_tool=AsyncMock()
            )

        sent = client.post.call_args.kwargs["json"]["tools"]
        assert sent == [
            {
                "name": "book_meeting",
                "description": "Book a meeting",
                "input_schema": _TOOLS[0]["parameters"],
            }
        ]

    async def test_runs_the_tool_and_threads_the_result_back(self) -> None:
        execute = AsyncMock(return_value='{"ok": true}')
        client = _client(
            _anthropic_tool_use("toolu_1", "book_meeting", {"day": "friday"}),
            _anthropic_text("Booked for Friday."),
        )

        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(
                _anthropic_config(), _MESSAGES, tools=_TOOLS, execute_tool=execute
            )

        assert out.text == "Booked for Friday."
        execute.assert_awaited_once_with("book_meeting", {"day": "friday"})

        sent = client.post.call_args.kwargs["json"]["messages"]
        assert sent[-2]["role"] == "assistant"
        # The whole content array goes back, thinking/text blocks included.
        assert any(b["type"] == "tool_use" for b in sent[-2]["content"])
        assert sent[-1] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"ok": true}',
                }
            ],
        }

    async def test_text_is_read_from_the_text_block_not_index_zero(self) -> None:
        """A response that leads with a non-text block used to raise KeyError —
        `data["content"][0]["text"]` assumed text always came first."""
        payload = {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "The answer."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client = _client(payload)
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(_anthropic_config(), _MESSAGES)

        assert out.text == "The answer."

    async def test_tokens_accumulate_across_rounds(self) -> None:
        client = _client(
            _anthropic_tool_use("toolu_1", "book_meeting", {}),
            _anthropic_text("done", tokens=10),
        )
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(
                _anthropic_config(),
                _MESSAGES,
                tools=_TOOLS,
                execute_tool=AsyncMock(return_value="{}"),
            )

        assert out.total_tokens == 3 + 4 + 10

    async def test_the_cap_withholds_tools_and_forces_an_answer(self) -> None:
        looping = [
            _anthropic_tool_use("t", "book_meeting", {})
        ] * _MAX_TOOL_ROUNDS
        client = _client(*looping, _anthropic_text("I couldn't finish."))

        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            out = await complete_text(
                _anthropic_config(),
                _MESSAGES,
                tools=_TOOLS,
                execute_tool=AsyncMock(return_value="{}"),
            )

        assert out.text == "I couldn't finish."
        assert "tools" not in client.post.call_args.kwargs["json"]


# --------------------------------------------------------------------------
# Bedrock
# --------------------------------------------------------------------------


def _converse_text(text: str, tokens: int = 5) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"totalTokens": tokens},
    }


def _converse_tool_use(tool_id: str, name: str, args: dict, tokens: int = 7) -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": "Checking."},
                    {"toolUse": {"toolUseId": tool_id, "name": name, "input": args}},
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"totalTokens": tokens},
    }


def _bedrock(*payloads: dict):
    """Patch boto3 and return the mock so kwargs can be inspected."""
    client = MagicMock()
    client.converse.side_effect = list(payloads)
    boto3 = MagicMock()
    boto3.client.return_value = client
    return client, patch.dict("sys.modules", {"boto3": boto3})


def _bedrock_config() -> LLMConfig:
    return LLMConfig(provider="bedrock", model="anthropic.claude-sonnet-5-v1:0")


@pytest.mark.unit
@pytest.mark.asyncio
class TestBedrockTools:
    async def test_no_tools_sends_no_toolconfig(self) -> None:
        client, boto = _bedrock(_converse_text("hi"))
        with boto:
            out = await complete_text(_bedrock_config(), _MESSAGES, aws=AWSConfig())

        assert out.text == "hi"
        assert "toolConfig" not in client.converse.call_args.kwargs

    async def test_tools_are_wrapped_in_toolspec(self) -> None:
        """botocore's own model: Tool -> toolSpec, and the schema goes under
        inputSchema.json."""
        client, boto = _bedrock(_converse_text("hi"))
        with boto:
            await complete_text(
                _bedrock_config(),
                _MESSAGES,
                aws=AWSConfig(),
                tools=_TOOLS,
                execute_tool=AsyncMock(),
            )

        assert client.converse.call_args.kwargs["toolConfig"] == {
            "tools": [
                {
                    "toolSpec": {
                        "name": "book_meeting",
                        "description": "Book a meeting",
                        "inputSchema": {"json": _TOOLS[0]["parameters"]},
                    }
                }
            ]
        }

    async def test_runs_the_tool_and_threads_the_result_back(self) -> None:
        execute = AsyncMock(return_value='{"ok": true}')
        client, boto = _bedrock(
            _converse_tool_use("tu_1", "book_meeting", {"day": "friday"}),
            _converse_text("Booked."),
        )

        with boto:
            out = await complete_text(
                _bedrock_config(),
                _MESSAGES,
                aws=AWSConfig(),
                tools=_TOOLS,
                execute_tool=execute,
            )

        assert out.text == "Booked."
        execute.assert_awaited_once_with("book_meeting", {"day": "friday"})

        sent = client.converse.call_args.kwargs["messages"]
        assert sent[-2]["role"] == "assistant"
        assert sent[-1] == {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "tu_1",
                        "content": [{"text": '{"ok": true}'}],
                    }
                }
            ],
        }

    async def test_tokens_accumulate_across_rounds(self) -> None:
        _client_unused, boto = _bedrock(
            _converse_tool_use("tu_1", "book_meeting", {}, tokens=7),
            _converse_text("done", tokens=11),
        )
        with boto:
            out = await complete_text(
                _bedrock_config(),
                _MESSAGES,
                aws=AWSConfig(),
                tools=_TOOLS,
                execute_tool=AsyncMock(return_value="{}"),
            )

        assert out.total_tokens == 18

    async def test_the_cap_withholds_tools_and_forces_an_answer(self) -> None:
        looping = [_converse_tool_use("t", "book_meeting", {})] * _MAX_TOOL_ROUNDS
        client, boto = _bedrock(*looping, _converse_text("I couldn't finish."))

        with boto:
            out = await complete_text(
                _bedrock_config(),
                _MESSAGES,
                aws=AWSConfig(),
                tools=_TOOLS,
                execute_tool=AsyncMock(return_value="{}"),
            )

        assert out.text == "I couldn't finish."
        assert "toolConfig" not in client.converse.call_args.kwargs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_unsupported_provider_warning_remains() -> None:
    """The warning was the placeholder for this PR — it must not outlive it."""
    client = _client(_anthropic_text("hi"))
    with (
        patch("turncall.services.llm_text.get_http_client", return_value=client),
        patch("turncall.services.llm_text.logger") as log,
    ):
        await complete_text(
            _anthropic_config(), _MESSAGES, tools=_TOOLS, execute_tool=AsyncMock()
        )

    assert not any(
        "unsupported" in str(c).lower() for c in log.warning.call_args_list
    )
