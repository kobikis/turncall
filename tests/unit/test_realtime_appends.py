"""An appended message reaches a Grok session (#388).

pipecat 1.13's Grok service answers an `LLMMessagesAppendFrame` only when the
frame itself reaches it. Queued at the pipeline's source — the S2S idle
check-in, an eval's typed caller turn — it was consumed by the user
aggregator, which pushed the context downstream instead, and Grok said
nothing (measured live, `tests/live/test_live_xai_tools.py`). The pair in
`realtime_appends` carries it around the user aggregator.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import LLMContextFrame, LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregator,
)
from pipecat.tests.utils import run_test

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import create_pipeline
from turncall.orchestrator.realtime_appends import (
    AppendsAroundTheUserAggregator,
    around_the_user_aggregator,
)

pytestmark = pytest.mark.unit

_TURN = {"role": "user", "content": "What's the special tonight?"}


def _processors(provider: str) -> list[Any]:
    config = AgentConfig.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": provider}}
    )
    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=False, records=False, has_database=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", google="g", xai="xai-key"),
    )
    return pipeline.processors


class TestTheFactory:
    def test_an_xai_pipeline_wraps_the_user_aggregator(self) -> None:
        processors = _processors("xai")
        user = next(
            i for i, p in enumerate(processors) if isinstance(p, LLMUserAggregator)
        )
        bypass = processors[user - 1]

        assert isinstance(bypass, AppendsAroundTheUserAggregator)
        assert processors[user + 1] is bypass.rejoin

    @pytest.mark.parametrize("provider", ["openai", "google"])
    def test_other_providers_are_left_alone(self, provider: str) -> None:
        """Gemini Live reads the message out of the downstream context; OpenAI
        Realtime is unmeasured here, so it is not changed blind."""
        processors = _processors(provider)

        assert not any(
            isinstance(p, AppendsAroundTheUserAggregator) for p in processors
        )


async def _through(around: tuple[list[Any], list[Any]]) -> tuple[list[Any], Any]:
    before, after = around
    context = LLMContext()
    user = LLMUserAggregator(context)
    down, _ = await run_test(
        Pipeline([*before, user, *after]),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_TURN], run_llm=True)],
    )
    carried = (LLMMessagesAppendFrame, LLMContextFrame)
    return [f for f in down if isinstance(f, carried)], context


@pytest.mark.asyncio
async def test_the_append_itself_goes_on_past_the_user_aggregator() -> None:
    """So the service sends it as an item, and the assistant aggregator
    records it and asks for the response, as pipecat's service expects."""
    down, context = await _through(around_the_user_aggregator("xai"))

    [frame] = down
    assert isinstance(frame, LLMMessagesAppendFrame)
    assert frame.messages == [_TURN] and frame.run_llm
    # Recorded once, by the assistant aggregator, not here too.
    assert context.get_messages() == []


@pytest.mark.asyncio
async def test_without_the_pair_the_user_aggregator_takes_it() -> None:
    """What the Grok session used to get: a context pushed downstream, which
    it reads as nothing to answer."""
    down, context = await _through(around_the_user_aggregator("google"))

    assert [type(f) for f in down] == [LLMContextFrame]
    assert context.get_messages() == [_TURN]
