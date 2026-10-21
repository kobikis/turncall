"""An appended message reaches a Grok or OpenAI Realtime session (#388, #390).

pipecat 1.13's Grok service answers an `LLMMessagesAppendFrame` only when the
frame itself reaches it. Queued at the pipeline's source — the S2S idle
check-in, an eval's typed caller turn — it was consumed by the user
aggregator, which pushed the context downstream instead, and Grok said
nothing (measured live, `tests/live/test_live_xai_tools.py`). The pair in
`realtime_appends` carries it around the user aggregator. OpenAI Realtime's
service handles the path the same way (#390).
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
    @pytest.mark.parametrize("provider", ["xai", "openai"])
    def test_the_pipeline_wraps_the_user_aggregator(self, provider: str) -> None:
        processors = _processors(provider)
        user = next(
            i for i, p in enumerate(processors) if isinstance(p, LLMUserAggregator)
        )
        bypass = processors[user - 1]

        assert isinstance(bypass, AppendsAroundTheUserAggregator)
        assert processors[user + 1] is bypass.rejoin

    @pytest.mark.parametrize("provider", ["google", "openai_live"])
    def test_other_providers_are_left_alone(self, provider: str) -> None:
        """Gemini Live reads the message out of the downstream context;
        gpt-live-1's check-in goes through `say()`."""
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


def _recording_service(provider: str) -> Any:
    """pipecat's own service, its websocket replaced by a list of what it sent,
    its session already set up — a call under way."""
    if provider == "xai":
        from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService as S
    else:
        from pipecat.services.openai.realtime.llm import (
            OpenAIRealtimeLLMService as S,
        )

    class Recording(S):  # type: ignore[misc, valid-type]
        async def _connect(self) -> None:
            self.sent: list[str] = []

        async def _disconnect(self) -> None:
            pass

        async def send_client_event(self, event: Any) -> None:
            self.sent.append(event.type)

    service = Recording(api_key="test")
    service._api_session_ready = True
    service._llm_needs_conversation_setup = False
    return service


async def _sent_for_an_append(provider: str, *, around: bool) -> list[str]:
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
    )

    context = LLMContext()
    pair = LLMContextAggregatorPair(context)
    service = _recording_service(provider)
    service._context = context
    before, after = around_the_user_aggregator(provider) if around else ([], [])
    await run_test(
        Pipeline([*before, pair.user(), *after, service, pair.assistant()]),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_TURN], run_llm=True)],
    )
    return service.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["xai", "openai"])
async def test_the_service_sends_the_message_and_asks_for_a_reply(
    provider: str,
) -> None:
    sent = await _sent_for_an_append(provider, around=True)

    assert sent == ["conversation.item.create", "response.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["xai", "openai"])
async def test_pipecat_still_needs_it(provider: str) -> None:
    """Without the pair the service sends nothing. Once this fails, pipecat
    answers a downstream context itself and the pair can go."""
    assert await _sent_for_an_append(provider, around=False) == []
