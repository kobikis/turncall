"""An S2S agent speaks first once its pipeline has started (#373).

Realtime, Gemini Live and Nova Sonic connect in `setup()`, and pipecat 1.13
holds the `StartFrame` at each processor until its own setup is done, then
fires `on_pipeline_started` once every processor has handled it. The
greeting used to wait a fixed two seconds instead: dead air on a fast
connect, and a guess on a slow one.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import Frame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.workers.runner import WorkerRunner

from turncall.orchestrator.call_session import CallSession

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
