"""The hangup gate ends the call after the reply that follows the tool (#240)."""

import pytest
from pipecat.frames.frames import (
    EndWorkerFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.tests.utils import run_test

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
