"""The Gemini dialect on the text path (#205): chat, SMS, WhatsApp text.

`complete_text` speaks raw HTTP, so the tool loop pipecat gives the voice path
exists here once per dialect. Gemini's: the assistant role is `model`, the
system prompt goes out of band, tools are `functionDeclarations`, results are
`functionResponse` parts — and Gemini 3 signs its function calls, so the
model's turn has to go back exactly as it came or the next round is a 400.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import LLMConfig
from turncall.services.credentials import Credentials
from turncall.services.llm_text import _MAX_TOOL_ROUNDS, complete_text

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TOOLS = [
    {
        "name": "lookup_order",
        "description": "Look up an order",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            # Outside the OpenAPI subset Gemini's `parameters` takes; the full
            # JSON Schema field accepts it.
            "additionalProperties": False,
        },
    }
]
_MESSAGES = [
    {"role": "system", "content": "You look up orders."},
    {"role": "user", "content": "Where is order 42?"},
    {"role": "assistant", "content": "Let me check."},
    {"role": "user", "content": "Thanks."},
]


def _reply(text: str, tokens: int = 5) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"totalTokenCount": tokens},
    }


def _wants(*calls: tuple[str, dict[str, Any]], tokens: int = 3) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"text": "Checking.", "thought": True}]
    for i, (name, args) in enumerate(calls):
        part: dict[str, Any] = {
            "functionCall": {"id": f"call-{i}", "name": name, "args": args}
        }
        if i == 0:
            # Gemini signs the first call of a batch.
            part["thoughtSignature"] = "c2lnbmVk"
        parts.append(part)
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}}],
        "usageMetadata": {"totalTokenCount": tokens},
    }


def _client(*payloads: dict[str, Any]) -> MagicMock:
    client = AsyncMock()
    responses = []
    for payload in payloads:
        response = MagicMock()
        response.json.return_value = payload
        responses.append(response)
    client.post.side_effect = responses
    return client


def _config(**kw: Any) -> LLMConfig:
    return LLMConfig(provider="google", **{"api_key": "g-key", **kw})


async def _complete(client: MagicMock, config: LLMConfig, **kw: Any) -> Any:
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        return await complete_text(config, _MESSAGES, **kw)


def _sent(client: MagicMock, call: int = -1) -> dict[str, Any]:
    return client.post.call_args_list[call].kwargs["json"]


class TestTheRequest:
    async def test_shape(self) -> None:
        client = _client(_reply("Shipped."))

        out = await _complete(client, _config(model="gemini-3.6-flash", max_tokens=99))

        assert out.text == "Shipped."
        assert out.total_tokens == 5
        call = client.post.call_args
        assert call.args[0] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent"
        )
        assert call.kwargs["headers"]["x-goog-api-key"] == "g-key"
        body = call.kwargs["json"]
        assert body["systemInstruction"] == {"parts": [{"text": "You look up orders."}]}
        assert body["contents"] == [
            {"role": "user", "parts": [{"text": "Where is order 42?"}]},
            {"role": "model", "parts": [{"text": "Let me check."}]},
            {"role": "user", "parts": [{"text": "Thanks."}]},
        ]
        assert body["generationConfig"]["maxOutputTokens"] == 99
        assert "tools" not in body

    async def test_the_house_model_when_none_is_named(self) -> None:
        from turncall.services.llm_models import resolve_llm_model

        client = _client(_reply("hi"))
        await _complete(client, _config())

        assert (
            f"/models/{resolve_llm_model('google', '')}:"
            in client.post.call_args.args[0]
        )

    async def test_a_models_prefix_is_not_doubled(self) -> None:
        client = _client(_reply("hi"))
        await _complete(client, _config(model="models/gemini-3.6-flash"))

        assert (
            "/models/gemini-3.6-flash:generateContent" in client.post.call_args.args[0]
        )

    async def test_gemini_3_gets_no_temperature(self) -> None:
        client = _client(_reply("hi"))
        await _complete(client, _config(model="gemini-3.6-flash", temperature=0.2))

        assert "temperature" not in _sent(client)["generationConfig"]

    async def test_an_older_model_does(self) -> None:
        client = _client(_reply("hi"))
        await _complete(client, _config(model="gemini-2.5-flash", temperature=0.2))

        assert _sent(client)["generationConfig"]["temperature"] == 0.2

    async def test_extra_merges_into_the_generation_config(self) -> None:
        client = _client(_reply("hi"))
        await _complete(
            client,
            _config(
                model="gemini-3.6-flash",
                extra={"temperature": 0.4, "thinkingConfig": {"thinkingLevel": "low"}},
            ),
        )

        generation = _sent(client)["generationConfig"]
        assert generation["temperature"] == 0.4
        assert generation["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_the_project_key_is_used(self) -> None:
        client = _client(_reply("hi"))
        credentials = Credentials(
            platform={"google": "platform"}, project={"google": {"api_key": "proj"}}
        )
        with patch("turncall.services.llm_text.get_http_client", return_value=client):
            await complete_text(
                LLMConfig(provider="google"), _MESSAGES, credentials=credentials
            )

        assert client.post.call_args.kwargs["headers"]["x-goog-api-key"] == "proj"

    async def test_a_blocked_prompt_is_an_empty_reply_not_a_crash(self) -> None:
        """A prompt the safety filter blocks comes back with no candidates."""
        client = _client({"promptFeedback": {"blockReason": "SAFETY"}})

        out = await _complete(client, _config())

        assert out.text == ""


class TestTools:
    async def test_declared_with_the_full_json_schema(self) -> None:
        client = _client(_reply("hi"))
        await _complete(client, _config(), tools=_TOOLS, execute_tool=AsyncMock())

        assert _sent(client)["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "lookup_order",
                        "description": "Look up an order",
                        "parametersJsonSchema": _TOOLS[0]["parameters"],
                    }
                ]
            }
        ]

    async def test_runs_the_tool_and_threads_the_result_back(self) -> None:
        execute = AsyncMock(return_value='{"status": "shipped"}')
        wants = _wants(("lookup_order", {"order_id": "42"}))
        client = _client(wants, _reply("It shipped.", tokens=7))

        out = await _complete(client, _config(), tools=_TOOLS, execute_tool=execute)

        assert out.text == "It shipped."
        assert out.total_tokens == 10
        execute.assert_awaited_once_with("lookup_order", {"order_id": "42"})
        contents = _sent(client)["contents"]
        # The model's turn verbatim — thought part and signature included.
        assert contents[-2] == wants["candidates"][0]["content"]
        assert contents[-1] == {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call-0",
                        "name": "lookup_order",
                        "response": {"output": '{"status": "shipped"}'},
                    }
                }
            ],
        }

    async def test_parallel_calls_answer_in_order(self) -> None:
        execute = AsyncMock(side_effect=["a", "b"])
        client = _client(
            _wants(
                ("lookup_order", {"order_id": "1"}), ("lookup_order", {"order_id": "2"})
            ),
            _reply("Both found."),
        )

        await _complete(client, _config(), tools=_TOOLS, execute_tool=execute)

        parts = _sent(client)["contents"][-1]["parts"]
        assert [p["functionResponse"]["response"]["output"] for p in parts] == [
            "a",
            "b",
        ]
        assert [p["functionResponse"]["id"] for p in parts] == ["call-0", "call-1"]

    async def test_a_call_without_an_id_sends_none(self) -> None:
        wants = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "lookup_order", "args": {}}}
                        ],
                    }
                }
            ]
        }
        client = _client(wants, _reply("ok"))

        await _complete(
            client, _config(), tools=_TOOLS, execute_tool=AsyncMock(return_value="r")
        )

        response = _sent(client)["contents"][-1]["parts"][0]["functionResponse"]
        assert "id" not in response

    async def test_the_thought_is_not_the_reply(self) -> None:
        payload = _reply("The answer.")
        payload["candidates"][0]["content"]["parts"].insert(
            0, {"text": "Let me think.", "thought": True}
        )
        client = _client(payload)

        out = await _complete(client, _config())

        assert out.text == "The answer."

    async def test_the_cap_withholds_the_tools_so_a_reply_goes_out(self) -> None:
        loops = [
            _wants(("lookup_order", {"order_id": "1"})) for _ in range(_MAX_TOOL_ROUNDS)
        ]
        client = _client(*loops, _reply("Here is what I have."))

        out = await _complete(
            client, _config(), tools=_TOOLS, execute_tool=AsyncMock(return_value="r")
        )

        assert out.text == "Here is what I have."
        assert client.post.await_count == _MAX_TOOL_ROUNDS + 1
        assert "tools" not in _sent(client)
