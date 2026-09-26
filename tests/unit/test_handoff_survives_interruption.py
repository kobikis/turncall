"""A handoff must be atomic with respect to barge-in.

test_handoff_tools.py covers what the handoff queues — the target's prompt and
the target's advertised tools. That is the wiring. This drives Pipecat's own
interruption drain over the frames the handoff actually queued and checks which
ones are still there afterwards: a caller talking mid-handoff must not leave
the agent running the previous agent's prompt and tools over a cleared context.

Pipecat 1.12 shipped the identical fix for Flows. TurnCall's handoff is the
same transition written by hand, which is why it did not get it.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://x/hook",
    )


class _SessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


async def _queue_a_handoff(queue):
    """Run the real handoff, landing its frames in a real pipecat FrameQueue."""
    from turncall.orchestrator.tool_bridge import (
        _apply_handoff_context,
        _load_handoff_target,
    )

    config = AgentConfig(tools=[_tool("refund")])
    agent = SimpleNamespace(name="Billing", config_blob=config.model_dump(mode="json"))
    call_context = SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        session_factory=_SessionFactory(),
        mcp_manager=None,
    )

    async def queue_frame(frame):
        queue.put_nowait(frame)

    params = SimpleNamespace(
        llm=MagicMock(),
        context=MagicMock(),
        pipeline_worker=SimpleNamespace(queue_frame=queue_frame),
    )

    with (
        patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            new=AsyncMock(return_value=agent),
        ),
        patch("turncall.orchestrator.tool_bridge.register_tools"),
    ):
        target = await _load_handoff_target({"agent_id": str(uuid4())}, call_context)
        assert target is not None
        await _apply_handoff_context(target, call_context, params)


def _drain(queue) -> list:
    """Pipecat's real interruption drain, then whatever survived it."""
    queue.reset()
    return list(queue._queue)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_handoff_frames_survive_an_interruption():
    from pipecat.frames.frames import LLMSetToolsFrame, LLMUpdateSettingsFrame
    from pipecat.utils.frame_queue import FrameQueue

    queue = FrameQueue()
    await _queue_a_handoff(queue)

    survivors = _drain(queue)

    assert any(isinstance(f, LLMUpdateSettingsFrame) for f in survivors), (
        "the target agent's system prompt was dropped by the interruption — "
        "the agent keeps answering as the previous agent over a cleared context"
    )
    tool_frames = [f for f in survivors if isinstance(f, LLMSetToolsFrame)]
    assert len(tool_frames) == 1, (
        "the tool swap was dropped by the interruption — the model still "
        "advertises the previous agent's tools"
    )
    assert [t.name for t in tool_frames[0].tools.standard_tools] == ["refund"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_spoken_frame_queued_alongside_is_still_dropped():
    """The exception is narrow. Being dropped on interruption is correct for
    speech — if the caller starts talking, the agent must stop talking."""
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.utils.frame_queue import FrameQueue

    queue = FrameQueue()
    await _queue_a_handoff(queue)
    queue.put_nowait(TTSSpeakFrame(text="Putting you through to billing."))

    survivors = _drain(queue)

    assert not any(isinstance(f, TTSSpeakFrame) for f in survivors), (
        "a spoken frame survived an interruption — the flag leaked out of "
        "the handoff and the agent talks over the caller"
    )


@pytest.mark.unit
def test_nothing_outside_the_handoff_is_marked_uninterruptible():
    """A state change that must be atomic, not a policy. If this fires, the
    new site needs the same justification the handoff has."""
    src = Path(__file__).resolve().parents[2] / "src" / "turncall"
    marked = sorted(
        {
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            for line in path.read_text().splitlines()
            if "interruptible = " in line and not line.lstrip().startswith("#")
        }
    )
    assert marked == ["orchestrator/tool_bridge.py"], marked


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cancelled_handoff_that_is_then_drained_still_lands_the_target_tools():
    """PRD #135's second test: the user-visible failure, stated as a test.

    The drain is only half of what an interruption does. Pipecat also cancels
    every in-flight function call registered with `cancel_on_interruption=True`
    — which `handoff_to_agent` is, being a sync tool — and a frame the handler
    never got to build cannot be saved by a flag. So both halves land here:
    cancellation is requested from inside `_build_tools_schema`, while the
    switch is still being assembled, and *then* the queue is drained.

    Since #144 the switch suspends nowhere once it starts mutating, so the
    cancellation cannot be delivered inside it and both frames are queued;
    the flag then carries them through the drain. Neither mechanism alone
    suffices, which is why they are exercised together here —
    test_handoff_switch_is_atomic.py owns the cancellation half on its own.
    """
    import asyncio

    from pipecat.frames.frames import LLMSetToolsFrame, LLMUpdateSettingsFrame
    from pipecat.utils.frame_queue import FrameQueue

    from turncall.orchestrator import pipeline_factory

    queue = FrameQueue()
    handoff: list[asyncio.Task] = []
    real_build = pipeline_factory._build_tools_schema

    def build_and_interrupt(config):
        # The caller starts talking: pipecat cancels the tool's task. Requested
        # here, between the prompt frame and the tools frame.
        handoff[0].cancel()
        return real_build(config)

    with patch.object(pipeline_factory, "_build_tools_schema", new=build_and_interrupt):
        handoff.append(asyncio.create_task(_queue_a_handoff(queue)))
        await asyncio.gather(*handoff, return_exceptions=True)

    survivors = _drain(queue)

    tool_frames = [f for f in survivors if isinstance(f, LLMSetToolsFrame)]
    assert any(isinstance(f, LLMUpdateSettingsFrame) for f in survivors), (
        "the prompt frame was lost to an interruption landing mid-handoff"
    )
    assert len(tool_frames) == 1, (
        "the tool swap was lost to an interruption landing between the two "
        "frames — the model still advertises the previous agent's tools, "
        "which is exactly the state the swap was added to prevent"
    )
    assert [t.name for t in tool_frames[0].tools.standard_tools] == ["refund"]
