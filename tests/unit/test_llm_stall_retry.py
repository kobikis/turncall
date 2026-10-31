"""A hosted LLM request that never starts answering is sent again (calls
c02c84fd, ebfa5e53): see `orchestrator/llm_stall.py`."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from tests.conftest import creds
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator import llm_stall
from turncall.orchestrator.llm_stall import StallRetryingOpenAILLMService
from turncall.orchestrator.pipeline_factory import _create_llm_service

pytestmark = pytest.mark.unit


def _service(provider: str, model: str = "m", **llm):
    config = AgentConfig(
        system_prompt="hi", llm=LLMConfig(provider=provider, model=model, **llm)
    )
    return _create_llm_service(config, creds("sk-test", openrouter="sk-or-test"))


@pytest.mark.parametrize(
    ("provider", "model"),
    [("openai", "gpt-5.4-mini"), ("openrouter", "openai/gpt-5.4-mini")],
)
def test_a_hosted_openai_llm_resends_a_stall(provider, model):
    assert isinstance(_service(provider, model=model), StallRetryingOpenAILLMService)


def test_an_openrouter_fallback_to_another_vendor_is_not_cut():
    """OpenRouter may answer with any of `fallback_models`."""
    service = _service(
        "openrouter",
        model="openai/gpt-5.4-mini",
        fallback_models=["deepseek/deepseek-r1"],
    )

    assert not isinstance(service, StallRetryingOpenAILLMService)


@pytest.mark.parametrize(
    ("provider", "model", "effort"),
    [
        ("openai", "gpt-5.1", "high"),  # asked to reason (5.4+ drops it beside tools)
        ("openai", "o4-mini", None),  # always reasons
        ("openai", "gpt-5-mini", None),
        ("openrouter", "openai/o3", None),
        ("openrouter", "deepseek/deepseek-r1", None),  # another vendor's
    ],
)
def test_a_model_that_reasons_first_is_not_cut(provider, model, effort):
    service = _service(provider, model=model, reasoning_effort=effort)

    assert not isinstance(service, StallRetryingOpenAILLMService)


async def test_stalls_are_sent_again_until_one_answers(monkeypatch):
    service = _service("openai")
    monkeypatch.setattr(llm_stall, "LLM_STALL_SECONDS", 0.05)
    answered = object()
    attempts = iter([None, None, answered])

    async def stall_twice(**_params):
        if (reply := next(attempts)) is None:
            await asyncio.sleep(3600)  # no headers, ever
        return reply

    create = AsyncMock(side_effect=stall_twice)
    monkeypatch.setattr(service._client.chat.completions, "create", create)

    assert await service.get_chat_completions(LLMContext()) is answered
    assert create.await_count == 3
