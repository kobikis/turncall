"""The hangup gate ends the call after the reply that follows the tool (#240)."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    EndWorkerFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from turncall.orchestrator import hangup
from turncall.orchestrator.hangup import HangupAfterReply, HangupArmFrame

pytestmark = pytest.mark.unit

GOODBYE = [
    LLMFullResponseStartFrame(),
    TextFrame("Goodbye!"),
    LLMFullResponseEndFrame(),
]
GOODBYE_TYPES = [LLMFullResponseStartFrame, TextFrame, LLMFullResponseEndFrame]


@pytest.mark.asyncio
async def test_ends_after_the_goodbye_that_follows_the_tool():
    await run_test(
        HangupAfterReply(),
        frames_to_send=[HangupArmFrame(), *GOODBYE],
        expected_down_frames=[*GOODBYE_TYPES, EndWorkerFrame],
    )


@pytest.mark.asyncio
async def test_the_tool_calls_own_reply_ending_late_does_not_end_the_call():
    # The response that carried the tool call can close after the tool armed
    # the gate. Ending there would cut off the goodbye still to come.
    await run_test(
        HangupAfterReply(),
        frames_to_send=[HangupArmFrame(), LLMFullResponseEndFrame(), *GOODBYE],
        expected_down_frames=[LLMFullResponseEndFrame, *GOODBYE_TYPES, EndWorkerFrame],
    )


@pytest.mark.asyncio
async def test_current_reply_arm_ends_after_the_reply_playing_now():
    # A client delegation that ended the call left gpt-live-1 nothing to
    # voice: the reply it is giving is the last (#262).
    await run_test(
        HangupAfterReply(),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            HangupArmFrame(current_reply=True),
            TextFrame("Goodbye!"),
            LLMFullResponseEndFrame(),
        ],
        expected_down_frames=[*GOODBYE_TYPES, EndWorkerFrame],
    )


@pytest.mark.asyncio
async def test_current_reply_arm_with_no_reply_playing_ends_at_once():
    await run_test(
        HangupAfterReply(),
        frames_to_send=[*GOODBYE, HangupArmFrame(current_reply=True)],
        expected_down_frames=[*GOODBYE_TYPES, EndWorkerFrame],
    )


@pytest.mark.asyncio
async def test_the_call_is_ended_once():
    # A delegate's late answer arriving after the gate already ended the call.
    await run_test(
        HangupAfterReply(),
        frames_to_send=[HangupArmFrame(), *GOODBYE, HangupArmFrame(current_reply=True)],
        expected_down_frames=[*GOODBYE_TYPES, EndWorkerFrame],
    )


def test_not_ending_until_armed():
    gate = HangupAfterReply()
    assert not gate.ending


@pytest.mark.asyncio
async def test_still_ending_after_the_call_was_ended():
    gate = HangupAfterReply()
    await run_test(
        gate,
        frames_to_send=[HangupArmFrame(), *GOODBYE],
        expected_down_frames=[*GOODBYE_TYPES, EndWorkerFrame],
    )
    assert gate.ending


def test_begin_ending_marks_the_call_without_arming():
    """A client delegate's tool: the live service arms it on relaying (#262)."""
    gate = HangupAfterReply()
    gate.begin_ending()
    assert gate.ending


@pytest.mark.asyncio
async def test_a_call_marked_ending_still_waits_for_the_arm():
    gate = HangupAfterReply()
    gate.begin_ending()
    await run_test(
        gate,
        frames_to_send=[*GOODBYE],
        expected_down_frames=[*GOODBYE_TYPES],
    )


def _queued(gate: HangupAfterReply) -> list[object]:
    """What the live service hands the gate from outside the pipeline."""
    queued: list[object] = []

    async def _queue(frame: object, *_: object) -> None:
        queued.append(frame)

    gate.push_frame = AsyncMock()
    gate.queue_frame = _queue
    return queued


@pytest.mark.asyncio
async def test_arm_when_quiet_waits_for_the_reply_playing():
    """A goodbye relayed into a reply still playing would run on in it."""
    gate = HangupAfterReply()
    queued = _queued(gate)
    down = FrameDirection.DOWNSTREAM
    await gate.process_frame(LLMFullResponseStartFrame(), down)

    arming = asyncio.ensure_future(gate.arm_when_quiet())
    await asyncio.sleep(0.01)
    assert not arming.done() and not queued

    await gate.process_frame(LLMFullResponseEndFrame(), down)
    await asyncio.wait_for(arming, 1)
    [arm] = queued
    assert isinstance(arm, HangupArmFrame) and not arm.current_reply


@pytest.mark.asyncio
async def test_arm_when_quiet_gives_up_waiting(monkeypatch):
    monkeypatch.setattr(hangup, "_QUIET_TIMEOUT_SECS", 0.01)
    gate = HangupAfterReply()
    queued = _queued(gate)
    await gate.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)

    await asyncio.wait_for(gate.arm_when_quiet(), 1)
    assert [type(f) for f in queued] == [HangupArmFrame]


@pytest.mark.asyncio
async def test_unarmed_replies_pass_untouched():
    await run_test(
        HangupAfterReply(),
        frames_to_send=[*GOODBYE, *GOODBYE],
        expected_down_frames=[*GOODBYE_TYPES, *GOODBYE_TYPES],
    )


@pytest.mark.parametrize("pipeline_mode", ["cascade", "s2s"])
def test_every_live_pipeline_has_the_gate_before_the_tts(mocker, pipeline_mode):
    """Ahead of the TTS and the output, so the `EndWorkerFrame` it pushes
    queues behind the goodbye on its way to the caller."""
    from tests.unit.test_recorder_placement import _processors

    processors, output = _processors(mocker, pipeline_mode)
    gates = [i for i, p in enumerate(processors) if isinstance(p, HangupAfterReply)]
    assert len(gates) == 1, f"{pipeline_mode}: expected one hangup gate"
    assert gates[0] < processors.index(output)
