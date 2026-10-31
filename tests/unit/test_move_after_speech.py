"""A move costs no extra inference (#445): `MoveAfterSpeech`, frame by frame.

The processor sits right after the LLM and decides, for a response that
reported a `move_to_step`, which of its results asks the aggregator for an
inference: none until the response's last call settles, then one — or none
when the response spoke and held only moves. Responses with no move pass
untouched, pipecat's own rule. What is asserted is what the aggregator reads
off each result: `properties.run_llm`.
"""

import pytest
from pipecat.frames.frames import (
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from turncall.orchestrator.procedures import MoveAfterSpeech

pytestmark = pytest.mark.unit

MOVE = "move_to_step"


def _started(*calls: tuple[str, str]) -> FunctionCallsStartedFrame:
    return FunctionCallsStartedFrame(
        function_calls=[
            FunctionCallFromLLM(
                function_name=name,
                tool_call_id=call_id,
                arguments={},
                context=LLMContext(),
            )
            for name, call_id in calls
        ]
    )


def _result(
    name: str, call_id: str, properties: FunctionCallResultProperties | None = None
) -> FunctionCallResultFrame:
    return FunctionCallResultFrame(
        function_name=name,
        tool_call_id=call_id,
        arguments={},
        result='{"success": true}',
        properties=properties,
    )


def _response(*texts: str) -> list:
    return [
        LLMFullResponseStartFrame(),
        *(LLMTextFrame(t) for t in texts),
        LLMFullResponseEndFrame(),
    ]


async def _run_llm(frames: list) -> dict[str, bool | None]:
    """Each result's `run_llm` as it leaves the processor, by call id."""
    down, _up = await run_test(MoveAfterSpeech(), frames_to_send=frames)
    return {
        f.tool_call_id: (f.properties.run_llm if f.properties else None)
        for f in down
        if isinstance(f, FunctionCallResultFrame)
    }


async def test_a_move_beside_speech_asks_for_no_inference():
    frames = [
        *_response("That's the internet light."),
        _started((MOVE, "m1")),
        _result(MOVE, "m1"),
    ]
    assert await _run_llm(frames) == {"m1": False}


async def test_a_move_with_no_speech_asks_for_one():
    frames = [*_response(), _started((MOVE, "m1")), _result(MOVE, "m1")]
    assert await _run_llm(frames) == {"m1": True}


async def test_whitespace_or_unspoken_text_is_not_speech():
    unspoken = LLMTextFrame("Not for the caller.")
    unspoken.skip_tts = True
    frames = [
        LLMFullResponseStartFrame(),
        LLMTextFrame("  "),
        unspoken,
        LLMFullResponseEndFrame(),
        _started((MOVE, "m1")),
        _result(MOVE, "m1"),
    ]
    assert await _run_llm(frames) == {"m1": True}


async def test_speech_from_an_earlier_response_does_not_count():
    frames = [
        *_response("Which lights are on?"),
        *_response(),
        _started((MOVE, "m1")),
        _result(MOVE, "m1"),
    ]
    assert await _run_llm(frames) == {"m1": True}


async def test_beside_another_tool_only_the_last_result_asks():
    # The move settles first, as a sync built-in beside a webhook does.
    frames = [
        *_response("Let me look at your router."),
        _started((MOVE, "m1"), ("router_status", "t1")),
        _result(MOVE, "m1"),
        _result("router_status", "t1"),
    ]
    assert await _run_llm(frames) == {"m1": False, "t1": True}


async def test_beside_another_tool_that_settled_first():
    frames = [
        *_response("Let me look at your router."),
        _started((MOVE, "m1"), ("router_status", "t1")),
        _result("router_status", "t1"),
        _result(MOVE, "m1"),
    ]
    assert await _run_llm(frames) == {"t1": False, "m1": True}


async def test_two_moves_beside_speech_ask_for_none():
    frames = [
        *_response("Okay."),
        _started((MOVE, "m1"), (MOVE, "m2")),
        _result(MOVE, "m1"),
        _result(MOVE, "m2"),
    ]
    assert await _run_llm(frames) == {"m1": False, "m2": False}


async def test_a_response_with_no_move_is_left_to_pipecat():
    frames = [
        *_response("Let me look."),
        _started(("router_status", "t1")),
        _result("router_status", "t1"),
    ]
    assert await _run_llm(frames) == {"t1": None}


async def test_a_tools_own_choice_is_kept():
    # `load_skill` asks for its inference itself, after its switch.
    own = FunctionCallResultProperties(run_llm=False)
    frames = [
        *_response(),
        _started((MOVE, "m1"), ("load_skill", "s1")),
        _result(MOVE, "m1"),
        _result("load_skill", "s1", own),
    ]
    assert await _run_llm(frames) == {"m1": False, "s1": False}


async def test_other_properties_survive():
    async def updated() -> None:
        return None

    frames = [
        *_response("Okay."),
        _started((MOVE, "m1")),
        _result(MOVE, "m1", FunctionCallResultProperties(on_context_updated=updated)),
    ]
    down, _up = await run_test(MoveAfterSpeech(), frames_to_send=frames)
    [result] = [f for f in down if isinstance(f, FunctionCallResultFrame)]
    assert result.properties.run_llm is False
    assert result.properties.on_context_updated is updated


async def test_the_result_keeps_its_identity():
    # Decided in place: a rebuilt frame would be a new frame to every
    # observer, with a new id and none of the original's metadata.
    sent = _result(MOVE, "m1")
    sent.metadata["mark"] = "kept"
    frames = [*_response("Okay."), _started((MOVE, "m1")), sent]
    down, _up = await run_test(MoveAfterSpeech(), frames_to_send=frames)
    [result] = [f for f in down if isinstance(f, FunctionCallResultFrame)]
    assert result.id == sent.id
    assert result.metadata == {"mark": "kept"}
    assert result.properties.run_llm is False


async def test_a_cancelled_sibling_does_not_hold_the_last_result():
    # A barge-in cancels the lookup; the async tool settles after it and is
    # then the response's last call, so it asks for the inference.
    frames = [
        *_response(),
        _started((MOVE, "m1"), ("router_status", "t1"), ("slow_lookup", "a1")),
        _result(MOVE, "m1"),
        FunctionCallCancelFrame(function_name="router_status", tool_call_id="t1"),
        _result("slow_lookup", "a1"),
    ]
    assert await _run_llm(frames) == {"m1": False, "a1": True}


async def test_an_interruption_forgets_the_unsettled_calls():
    # What is still unsettled after a barge-in is pipecat's to decide, and
    # nothing is kept for a call that will never settle.
    processor = MoveAfterSpeech()
    frames = [
        *_response(),
        _started((MOVE, "m1"), ("router_status", "t1")),
        InterruptionFrame(),
        _result("router_status", "t1"),
    ]
    down, _up = await run_test(processor, frames_to_send=frames)
    [result] = [f for f in down if isinstance(f, FunctionCallResultFrame)]
    assert result.properties is None
    assert processor.unsettled == 0


async def test_a_cancelled_call_is_forgotten():
    processor = MoveAfterSpeech()
    frames = [
        *_response(),
        _started((MOVE, "m1")),
        FunctionCallCancelFrame(function_name=MOVE, tool_call_id="m1"),
    ]
    await run_test(processor, frames_to_send=frames)
    assert processor.unsettled == 0
