"""The idle hold for a gpt-live-1 client delegation (#257), against pipecat's own.

pipecat 1.17's idle timer stops for background work — a run of frames
(`BackgroundWorkStartedFrame` … `BackgroundWorkFinishedFrame`) that an async
function call and an `LLMWithBackend` delegation broadcast. gpt-live-1's client
delegation is neither: `OpenAILiveLLMService` hands the work to its
`BackendLLMWorker` and broadcasts nothing, so the timer the user aggregator
runs keeps going while the caller waits on the delegate. TurnCall's hold
(`CallSession._arm_idle_guard` reading `delegating`) is what keeps that silence
from being a strike.

Driven at the built live service: a real `create_pipeline`, then the server
event gpt-live-1 sends and a tool call its delegate reports. Every frame the
service sends upstream — where the user aggregator sits — is handed to a real
`UserIdleController`, as the aggregator hands it every frame it sees.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import BotStoppedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.live import events
from pipecat.services.openai.live.llm import OpenAILiveLLMService
from pipecat.turns.user_idle_controller import UserIdleController
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.workers.llm.backend_llm_worker import BackendToolCall

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator import delegation_events
from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline

_IDLE_SECS = 0.05
_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": {"type": "object", "properties": {}},
    "webhook_url": "https://example.com/book",
}


async def _controller() -> tuple[UserIdleController, list[bool]]:
    """The aggregator's idle timer; each firing records whether gpt-live-1 was delegating."""
    controller = UserIdleController(user_idle_timeout=_IDLE_SECS)
    await controller.setup(SimpleNamespace(task_manager=TaskManager()))
    return controller, []


def _live(controller: UserIdleController) -> Any:
    """gpt-live-1 in client mode, its upstream frames reaching `controller`."""
    agent = AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live"},
            "tools": [_BOOK],
        }
    )
    context = CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        stream_sid="MZ1",
        session_factory=MagicMock(),
    )
    with patch("turncall.orchestrator.call_recorder.attach_recorder"):
        pipeline = create_pipeline(
            config=agent,
            transport=MagicMock(),
            call_context=context,
            pipecat_settings=SimpleNamespace(),
            credentials=creds("sk-test"),
        )
    live = next(p for p in pipeline.processors if isinstance(p, OpenAILiveLLMService))

    async def _push(
        frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        # The user aggregator is upstream of the live service; nothing it
        # pushes downstream ever reaches the aggregator's idle timer.
        if direction == FrameDirection.UPSTREAM:
            await controller.process_frame(frame)

    live.push_frame = _push
    live.send_client_event = AsyncMock()
    live._setup = MagicMock()
    # The delegate's session, attached and working: it has not answered yet.
    live._backend_session = SimpleNamespace(send=AsyncMock())
    live._backend_attached.set()
    live.tasks = []

    def _create_task(coro: Any, name: str | None = None, *_: Any) -> Any:
        live.tasks.append(asyncio.ensure_future(coro))
        return live.tasks[-1]

    live.create_task = _create_task
    return live


async def _idle_during_a_delegation() -> list[bool]:
    """Whether the idle timer fired while the delegate worked, and `delegating` each time."""
    controller, fired = await _controller()
    live = _live(controller)

    @controller.event_handler("on_user_turn_idle")
    async def _idle(_controller: UserIdleController) -> None:
        fired.append(live.delegating)

    # gpt-live-1 says "one moment" and stops: the timer starts.
    await controller.process_frame(BotStoppedSpeakingFrame())
    # It hands the work over, and the delegate calls a tool on the way.
    with patch.object(delegation_events, "store_call_event", new=AsyncMock()):
        await live._handle_server_event(
            events.SessionDelegationCreatedEvent(
                type="session.delegation.created",
                delegation=events.DelegationMetadata(id="d1", target="client"),
            )
        )
        await live._on_backend_event(
            BackendToolCall(
                phase="started", function_name="book_table", tool_call_id="t1"
            )
        )
        # The delegate is still at work when the caller's silence runs out.
        await asyncio.sleep(_IDLE_SECS * 4)
    assert live.delegating
    await controller.stop()
    for task in live.tasks:
        task.cancel()
    await asyncio.gather(*live.tasks, return_exceptions=True)
    return fired


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipecat_still_needs_it() -> None:
    """pipecat 1.17 announces no background work for a client delegation, so
    its idle timer fires while the caller waits on the delegate — once, with
    `delegating` true, which is what the hold reads to make it no strike
    (`test_idle_caller.TestADelegateAtWork`). When this fails, pipecat holds
    the timer itself: delete the hold in `CallSession._arm_idle_guard` and
    keep this file's driver as the guard."""
    assert await _idle_during_a_delegation() == [True]
