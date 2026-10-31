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


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_a_hosted_openai_family_llm_resends_a_stall(provider):
    assert isinstance(_service(provider), StallRetryingOpenAILLMService)


def test_a_model_asked_to_reason_first_is_not_cut():
    service = _service("openai", model="o4-mini", reasoning_effort="high")

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
