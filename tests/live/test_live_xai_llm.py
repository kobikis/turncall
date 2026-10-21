"""Grok, the `xai` LLM provider, against xAI itself (#393).

The unit suite mocks the client, so this is what says the house model is one
xAI serves, through the service a voice call builds and pipecat's own
completion path.

Live: needs XAI_API_KEY with access to the chat endpoint. Skipped without
one. A voice-only key (ACL `api-key:endpoint:voice`) is refused with 403,
which this test reports as AUTHORIZATION: that is the key, not the model.
"""

from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import ErrorFrame, LLMContextFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from tests.conftest import creds

from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import _create_llm_service

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def test_grok_answers_on_its_house_model(xai_key: str) -> None:
    config = AgentConfig(llm=LLMConfig(provider="xai", max_tokens=16))
    service = _create_llm_service(
        config, creds(xai=xai_key), system_instruction="Answer in one word."
    )
    service.push_frame = AsyncMock()
    context = LLMContext(messages=[{"role": "user", "content": "Say hello."}])

    await service.process_frame(
        LLMContextFrame(context=context), FrameDirection.DOWNSTREAM
    )

    pushed = [c.args[0] for c in service.push_frame.await_args_list]
    errors = [f for f in pushed if isinstance(f, ErrorFrame)]
    assert not errors, (
        f"{service._settings.model}: {errors[0].category} {errors[0].error}"
    )
    assert "".join(f.text for f in pushed if isinstance(f, LLMTextFrame)).strip()
