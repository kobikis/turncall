"""Tool calling on the text path (SMS, Chat API, WhatsApp text).

complete_text() speaks raw HTTP rather than building a Pipecat pipeline, so
the tool loop the voice path gets from Pipecat has to exist here too. Tools
are opt-in: call_analysis and transfer briefings share this function and must
keep sending exactly the bytes they send today.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import LLMConfig
from turncall.services.llm_text import _MAX_TOOL_ROUNDS, complete_text

_TOOLS = [
    {
        "name": "book_meeting",
        "description": "Book a meeting",
        "parameters": {"type": "object", "properties": {"day": {"type": "string"}}},
    }
]


def _resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    return r


def _reply(text: str, tokens: int = 7) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": tokens},
    }


def _wants(*calls: tuple[str, str, dict], tokens: int = 11) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for cid, name, args in calls
                    ],
                }
            }
        ],
        "usage": {"total_tokens": tokens},
    }


def _client(*responses: dict) -> MagicMock:
    client = AsyncMock()
    client.post.side_effect = [_resp(r) for r in responses]
    return client


def _config() -> LLMConfig:
    return LLMConfig(provider="openai", model="gpt-4o-mini", api_key="k")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_tools_leaves_the_request_untouched():
    """The guard for call_analysis and transfer: they pass no tools and must
    keep producing a body without a `tools` key."""
    client = _client(_reply("hi"))
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(_config(), [{"role": "user", "content": "hi"}])

    assert out.text == "hi"
    assert "tools" not in client.post.call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tools_are_sent_in_openai_function_shape():
    client = _client(_reply("hi"))
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        await complete_text(
            _config(),
            [{"role": "user", "content": "hi"}],
            tools=_TOOLS,
            execute_tool=AsyncMock(),
        )

    sent = client.post.call_args.kwargs["json"]["tools"]
    assert sent == [{"type": "function", "function": _TOOLS[0]}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runs_a_tool_then_returns_the_follow_up_reply():
    execute = AsyncMock(return_value='{"ok": true}')
    client = _client(
        _wants(("call_1", "book_meeting", {"day": "friday"})),
        _reply("Booked for Friday."),
    )

    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "book friday"}],
            tools=_TOOLS,
            execute_tool=execute,
        )

    assert out.text == "Booked for Friday."
    execute.assert_awaited_once_with("book_meeting", {"day": "friday"})

    # The second request must carry the assistant's tool_calls turn and the
    # result, or the model has no idea what happened.
    second = client.post.call_args.kwargs["json"]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call_1"
    assert second[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true}',
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handles_several_tool_calls_in_one_round():
    execute = AsyncMock(side_effect=["a", "b"])
    client = _client(
        _wants(
            ("c1", "book_meeting", {"day": "mon"}),
            ("c2", "book_meeting", {"day": "tue"}),
        ),
        _reply("Both booked."),
    )

    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "book both"}],
            tools=_TOOLS,
            execute_tool=execute,
        )

    assert out.text == "Both booked."
    assert execute.await_count == 2
    results = client.post.call_args.kwargs["json"]["messages"][-2:]
    assert [m["tool_call_id"] for m in results] == ["c1", "c2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tokens_accumulate_across_rounds():
    client = _client(
        _wants(("c1", "book_meeting", {}), tokens=11),
        _reply("done", tokens=7),
    )
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "go"}],
            tools=_TOOLS,
            execute_tool=AsyncMock(return_value="{}"),
        )

    assert out.total_tokens == 18


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_model_that_never_stops_still_gets_a_reply():
    """Text has no turn-taking pressure, so nothing naturally ends the loop.
    At the cap we withhold the tools and let it answer in words — an SMS with
    a mediocre answer beats an SMS that never arrives."""
    looping = [_wants(("c", "book_meeting", {}))] * _MAX_TOOL_ROUNDS
    client = _client(*looping, _reply("Sorry, I couldn't finish that."))
    execute = AsyncMock(return_value="{}")

    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "go"}],
            tools=_TOOLS,
            execute_tool=execute,
        )

    assert out.text == "Sorry, I couldn't finish that."
    assert execute.await_count == _MAX_TOOL_ROUNDS
    # Final request drops `tools`, so the model has no way to ask for another.
    assert "tools" not in client.post.call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_back_not_raised():
    """execute_tool owns its errors; the loop just relays whatever it returns
    so the model can apologise or try something else."""
    execute = AsyncMock(return_value='{"error": "CRM timed out"}')
    client = _client(
        _wants(("c1", "book_meeting", {})),
        _reply("I couldn't reach the calendar."),
    )

    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "book"}],
            tools=_TOOLS,
            execute_tool=execute,
        )

    assert out.text == "I couldn't reach the calendar."
    assert "CRM timed out" in client.post.call_args.kwargs["json"]["messages"][-1]["content"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calls_in_one_round_run_concurrently():
    """Awaiting them in turn makes the reply wait for the sum of their
    latencies rather than the slowest — on SMS that competes with the
    provider's delivery timeout."""
    import asyncio

    running = 0
    peak = 0

    async def _slow(_name, _args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "{}"

    client = _client(
        _wants(
            ("c1", "book_meeting", {}),
            ("c2", "book_meeting", {}),
            ("c3", "book_meeting", {}),
        ),
        _reply("all done"),
    )
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        out = await complete_text(
            _config(),
            [{"role": "user", "content": "go"}],
            tools=_TOOLS,
            execute_tool=_slow,
        )

    assert out.text == "all done"
    assert peak == 3, f"tool calls ran {peak} at a time, expected 3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_results_keep_request_order():
    """tool_call_id pairing is positional in the loop, so order matters even
    when the calls finish out of order."""
    import asyncio

    async def _varied(_name, args):
        await asyncio.sleep(args["delay"])
        return args["tag"]

    client = _client(
        _wants(
            ("c1", "book_meeting", {"delay": 0.03, "tag": "first"}),
            ("c2", "book_meeting", {"delay": 0.0, "tag": "second"}),
        ),
        _reply("ok"),
    )
    with patch("turncall.services.llm_text.get_http_client", return_value=client):
        await complete_text(
            _config(),
            [{"role": "user", "content": "go"}],
            tools=_TOOLS,
            execute_tool=_varied,
        )

    sent = client.post.call_args.kwargs["json"]["messages"][-2:]
    assert [(m["tool_call_id"], m["content"]) for m in sent] == [
        ("c1", "first"),
        ("c2", "second"),
    ]
