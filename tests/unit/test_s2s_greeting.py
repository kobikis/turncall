"""An S2S agent speaks first once its pipeline has started (#373).

Realtime, Gemini Live and Nova Sonic connect in `setup()`, and pipecat 1.13
holds the `StartFrame` at each processor until its own setup is done, then
fires `on_pipeline_started` once every processor has handled it. The
greeting used to wait a fixed two seconds instead: dead air on a fast
connect, and a guess on a slow one.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import Frame, LLMContextFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.workers.runner import WorkerRunner

from tests.conftest import creds
from turncall.domain.models import AgentConfig, AWSConfig, S2SConfig
from turncall.orchestrator.call_session import CallSession, s2s_greeting_frame
from turncall.orchestrator.pipeline_factory import create_pipeline

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _SlowToConnect(FrameProcessor):
    """A realtime service that takes `delay` seconds to connect in setup()."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay
        self.connected_at: float | None = None
        self.greeted_at: float | None = None
        self.greeted = asyncio.Event()

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        await asyncio.sleep(self._delay)
        self.connected_at = time.monotonic()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMRunFrame):
            self.greeted_at = time.monotonic()
            self.greeted.set()
        await self.push_frame(frame, direction)


def _session(provider: str) -> CallSession:
    return CallSession(
        call_context=MagicMock(),
        transport=MagicMock(),
        pipeline=MagicMock(),
        pipeline_mode="s2s",
        s2s_provider=provider,
    )


async def _greet(provider: str, connect_delay: float) -> tuple[_SlowToConnect, float]:
    """Run a pipeline whose service connects in `connect_delay`, triggering
    the greeting the way `CallSession.start()` does for a carrier's call."""
    service = _SlowToConnect(connect_delay)
    worker = PipelineWorker(Pipeline([service]), cancel_on_idle_timeout=False)
    session = _session(provider)
    session._watch_pipeline_start(worker)
    runner = WorkerRunner()
    await runner.add_workers(worker)

    began = time.monotonic()
    await session._trigger_first_response(worker)
    run = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(service.greeted.wait(), timeout=connect_delay + 1.0)
    finally:
        await runner.cancel(reason="test over")
        await run
    return service, began


@pytest.mark.parametrize("provider", ["openai", "google", "aws"])
async def test_the_greeting_follows_the_connect_without_a_fixed_wait(
    provider: str,
) -> None:
    service, began = await _greet(provider, connect_delay=0.2)

    assert service.connected_at is not None and service.greeted_at is not None
    assert service.greeted_at >= service.connected_at
    # The old two-second floor is gone: the greeting goes as soon as the
    # pipeline is up.
    assert service.greeted_at - began < 1.0


async def test_the_greeting_still_plays_when_the_provider_is_slow_to_connect() -> None:
    """Slower than the two seconds the old wait assumed."""
    service, _ = await _greet("openai", connect_delay=2.5)

    assert service.connected_at is not None and service.greeted_at is not None
    assert service.greeted_at >= service.connected_at


# What the greeting asks for (#381). The frame above runs the pipeline's own
# context; whether the model answers it is the provider's. Gemini Live and
# Nova Sonic answered an empty one with nothing, so the caller spoke first.


async def _opening_context(config: AgentConfig) -> tuple[Any, Any]:
    """The context a call's S2S session opens on, and the built service: the
    greeting frame through the real user aggregator, as `CallSession` sends it."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.llm_service import LLMService
    from pipecat.tests.utils import run_test

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", google="g-test"),
    )
    aggregator = next(
        p for p in pipeline.processors if isinstance(p, LLMUserAggregator)
    )
    service = next(
        p for p in pipeline.processors_with_metrics() if isinstance(p, LLMService)
    )
    down, _ = await run_test(aggregator, frames_to_send=[s2s_greeting_frame()])
    (context,) = [f.context for f in down if isinstance(f, LLMContextFrame)]
    return context, service


def _agent(provider: str, first_message: str | None = None) -> AgentConfig:
    return AgentConfig(
        system_prompt="You are a receptionist.",
        first_message=first_message,
        pipeline_mode="s2s",
        s2s=S2SConfig(provider=provider),  # type: ignore[arg-type]
        aws=AWSConfig(access_key_id="AKIATEST", secret_access_key="shh"),
    )


async def _gemini_opening(config: AgentConfig) -> tuple[Any, Any]:
    """What Gemini Live sends once its session is up and the context arrives:
    the seeded turns, and whether it asked the model to answer them."""
    context, service = await _opening_context(config)
    session = MagicMock(
        send_client_content=AsyncMock(), send_realtime_input=AsyncMock()
    )
    service._session = session
    await service._handle_context(context)
    return session.send_client_content, session.send_realtime_input


async def _nova_sonic_interactive_text(config: AgentConfig) -> list[str]:
    """The text Nova Sonic sends as the user's interactive turn — the one it
    answers — when it finishes connecting on the call's context."""
    context, service = await _opening_context(config)
    service._ready_to_send_context = True
    service._send_prompt_start_event = AsyncMock()
    service._send_audio_input_start_event = AsyncMock()
    service._send_text_event = AsyncMock()
    service._receive_task_handler = MagicMock()
    service.create_task = MagicMock()
    service._sc = MagicMock()
    await service._handle_context(context)
    return [
        c.kwargs["text"]
        for c in service._send_text_event.await_args_list
        if c.kwargs.get("interactive")
    ]


class TestGeminiLiveSpeaksFirst:
    """pipecat's adapter returns the init system instruction as a message for
    an empty context, so the service skipped its own seed and then found
    nothing to answer (#381)."""

    async def test_first_message_is_asked_for(self) -> None:
        sent, nudge = await _gemini_opening(_agent("google", "Thanks for calling!"))

        sent.assert_awaited_once()
        assert sent.await_args.kwargs["turn_complete"] is True
        last = sent.await_args.kwargs["turns"][-1]
        assert last.role == "user"
        assert "Thanks for calling!" in last.parts[0].text
        nudge.assert_awaited()  # Gemini 3 runs no inference without one

    async def test_without_a_first_message_it_still_greets(self) -> None:
        sent, _ = await _gemini_opening(_agent("google"))

        sent.assert_awaited_once()
        assert sent.await_args.kwargs["turn_complete"] is True


class TestNovaSonicSpeaksFirst:
    """Nova Sonic 2 answers only a context that ends in a user message (#381)."""

    async def test_first_message_is_asked_for(self) -> None:
        (text,) = await _nova_sonic_interactive_text(
            _agent("aws", "Thanks for calling!")
        )
        assert "Thanks for calling!" in text

    async def test_without_a_first_message_it_still_greets(self) -> None:
        assert len(await _nova_sonic_interactive_text(_agent("aws"))) == 1


async def test_realtime_still_opens_on_an_empty_context() -> None:
    """Realtime greets from the empty run; nothing is added for it."""
    context, _ = await _opening_context(_agent("openai", "Hi!"))
    assert context.get_messages() == []
