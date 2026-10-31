"""A hosted LLM request that never answers is re-sent (call c02c84fd).

Twice in one call OpenAI accepted a chat completion and sent nothing for
16-17 s, until the caller's "Hello?" cancelled it: the caller heard silence.
A stall shows as no response headers at all, so pipecat's `retry_on_timeout`
(a time limit on the request's start, then one unbounded retry) covers it
without cutting a slow but streaming reply.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from tests.conftest import creds
from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import (
    LLM_STALL_SECONDS,
    _create_llm_service,
)

pytestmark = pytest.mark.unit


def _service(provider: str):
    config = AgentConfig(
        system_prompt="hi", llm=LLMConfig(provider=provider, model="m")
    )
    return _create_llm_service(config, creds("sk-test", openrouter="sk-or-test"))


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_a_hosted_openai_family_llm_retries_a_stalled_request(provider):
    service = _service(provider)

    assert service._retry_on_timeout is True
    assert service._retry_timeout_secs == LLM_STALL_SECONDS


async def test_a_stalled_request_is_sent_again(monkeypatch):
    service = _service("openai")
    monkeypatch.setattr(service, "_retry_timeout_secs", 0.05)
    answered = object()
    attempts = iter([None, answered])

    async def stall_then_answer(**_params):
        reply = next(attempts)
        if reply is None:
            await asyncio.sleep(3600)  # no headers, ever
        return reply

    create = AsyncMock(side_effect=stall_then_answer)
    monkeypatch.setattr(service._client.chat.completions, "create", create)

    assert await service.get_chat_completions(LLMContext()) is answered
    assert create.await_count == 2
