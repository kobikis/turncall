"""The `google` LLM provider against Google's real API (#205).

The unit tests replace Google's servers with a script; this is the check that
the script matches. Three things only Google can answer: that the house model
exists, that a follow-up carrying a signed function call and its result is
accepted (Gemini 3 refuses one that drops the signature), and that a model
Google has never heard of is an error pipecat marks unusable — which is what
lets a failover group (#222) move off it.
"""

from typing import Any

import pytest
from tests.conftest import creds

from turncall.domain.models import AgentConfig, LLMConfig
from turncall.services.credentials import Credentials
from turncall.services.llm_text import complete_text

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_TOOLS = [
    {
        "name": "lookup_order",
        "description": "Look up an order's shipping status by its id.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    }
]


def _keys(google_key: str) -> Credentials:
    return Credentials(platform={"google": google_key})


async def test_the_house_model_answers_a_chat_turn(google_key: str) -> None:
    out = await complete_text(
        LLMConfig(provider="google", max_tokens=64),
        [{"role": "user", "content": "Say hello in one word."}],
        credentials=_keys(google_key),
    )

    assert out.text.strip(), "the house model returned nothing"


async def test_a_chat_turn_calls_the_tool_and_answers_from_it(
    google_key: str,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, args: dict[str, Any]) -> str:
        calls.append((name, args))
        return '{"status": "shipped", "carrier": "Parcelwise"}'

    out = await complete_text(
        LLMConfig(provider="google", max_tokens=256),
        [
            {"role": "system", "content": "Use lookup_order for any order question."},
            {"role": "user", "content": "Which carrier has order 42?"},
        ],
        tools=_TOOLS,
        execute_tool=execute,
        credentials=_keys(google_key),
    )

    assert calls and calls[0][0] == "lookup_order", calls
    assert "parcelwise" in out.text.lower(), out.text


async def test_a_completion_runs_through_the_pipecat_service(google_key: str) -> None:
    from pipecat.processors.aggregators.llm_context import LLMContext

    from turncall.orchestrator.pipeline_factory import _create_llm_service

    service = _create_llm_service(
        AgentConfig(llm=LLMConfig(provider="google", max_tokens=64)),
        creds("", google=google_key),
        system_instruction="Answer in one word.",
    )

    reply = await service.run_inference(
        LLMContext(messages=[{"role": "user", "content": "Say hello."}])
    )

    assert reply and reply.strip(), "the Google service returned nothing"


async def test_an_unknown_model_is_an_error_pipecat_treats_as_permanent(
    google_key: str,
) -> None:
    """The failover precondition: only a permanent category moves a group on."""
    from pipecat.utils.errors import classify_http_exception

    from turncall.orchestrator.pipeline_factory import _create_llm_service

    service = _create_llm_service(
        AgentConfig(llm=LLMConfig(provider="google", model="gemini-no-such-model")),
        creds("", google=google_key),
    )
    from pipecat.processors.aggregators.llm_context import LLMContext

    with pytest.raises(Exception) as caught:
        await service.run_inference(
            LLMContext(messages=[{"role": "user", "content": "hi"}])
        )

    assert classify_http_exception(caught.value).is_permanent, caught.value
