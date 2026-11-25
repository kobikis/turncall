"""The procedure window and the move rule across a barge-in, through pipecat's
own context aggregators (#499).

pipecat 1.17 closes an interrupted assistant turn after the `InterruptionFrame`
rather than before it: the words already played are added behind the text
still queued in the aggregator, and the turn's context write comes later than
it did. The window (#442) and `MoveAfterSpeech` (#445) sit between the two
aggregators and share the context with them, so these run the real pair
around them — user aggregator, window, a scripted LLM, the move rule,
assistant aggregator — and read what the LLM was handed on each inference.
"""

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
from pipecat.frames.frames import (
    Frame,
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test

from tests.procedure_graphs import NO_INTERNET
from turncall.domain.procedure_state import ProcedureState, move
from turncall.domain.procedure_window import WINDOW_MARKER
from turncall.domain.procedures import MOVE_TO_STEP
from turncall.orchestrator.procedures import (
    CallProcedures,
    MoveAfterSpeech,
    ProcedureWindowProcessor,
)
from turncall.orchestrator.turn_message import is_marked, text_of

pytestmark = pytest.mark.unit

_GROUP = "response-1"
_BEAT = SleepFrame(sleep=0.1)

Answer = Callable[[], list[Frame]]


class _ScriptedLLM(FrameProcessor):
    """Where the LLM sits. Each context it is handed is one inference: it
    keeps what the context held then, and answers with the next entry of its
    script. On a barge-in it cancels the sync calls still running, as
    pipecat's LLM service does."""

    def __init__(self, script: list[Answer]) -> None:
        super().__init__()
        self._script = list(script)
        self.read: list[list[dict[str, Any]]] = []
        self._sync: dict[str, str] = {}  # tool_call_id -> function name

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.read.append(copy.deepcopy(frame.context.get_messages()))
            for out in self._script.pop(0)() if self._script else []:
                if (
                    isinstance(out, FunctionCallInProgressFrame)
                    and out.cancel_on_interruption
                ):
                    self._sync[out.tool_call_id] = out.function_name
                elif isinstance(out, FunctionCallResultFrame):
                    self._sync.pop(out.tool_call_id, None)
                await self.push_frame(out)
            return
        await self.push_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            for call_id, name in self._sync.items():
                await self.push_frame(
                    FunctionCallCancelFrame(function_name=name, tool_call_id=call_id)
                )
            self._sync.clear()


def _caller(text: str) -> LLMMessagesAppendFrame:
    """The caller's turn, written by the user aggregator, which then hands
    the context on as it does at a turn's end."""
    return LLMMessagesAppendFrame([{"role": "user", "content": text}], run_llm=True)


def _calls(*calls: tuple[str, str, bool]) -> list[Frame]:
    """A response's calls, as pipecat's LLM reports them: the list, then each
    in progress. `(name, id, sync)`."""
    started = FunctionCallsStartedFrame(
        function_calls=[
            FunctionCallFromLLM(
                function_name=name,
                tool_call_id=call_id,
                arguments={},
                context=LLMContext(),
            )
            for name, call_id, _ in calls
        ]
    )
    return [
        started,
        *(
            FunctionCallInProgressFrame(
                function_name=name,
                tool_call_id=call_id,
                arguments={},
                cancel_on_interruption=sync,
                group_id=_GROUP,
            )
            for name, call_id, sync in calls
        ),
    ]


def _result(name: str, call_id: str, result: Any) -> FunctionCallResultFrame:
    return FunctionCallResultFrame(
        function_name=name, tool_call_id=call_id, arguments={}, result=result
    )


async def _call(script: list[Answer], frames: list[Frame], holder: CallProcedures):
    context = LLMContext(
        [{"role": "system", "content": "You are home internet support."}]
    )
    pair = LLMContextAggregatorPair(context)
    llm, rule = _ScriptedLLM(script), MoveAfterSpeech()
    pipeline = Pipeline(
        [pair.user(), ProcedureWindowProcessor(holder), llm, rule, pair.assistant()]
    )
    await run_test(pipeline, frames_to_send=[*frames, _BEAT])
    return llm, rule


def _holder() -> CallProcedures:
    return CallProcedures(ProcedureState.at_call_start([NO_INTERNET]))


def _moved(holder: CallProcedures, to: str) -> Callable[[], None]:
    def apply() -> None:
        holder.replace(move(holder.state, NO_INTERNET.name, to).state)

    return apply


def _assert_window_before_the_turn(
    messages: list[dict[str, Any]], step_text: str
) -> None:
    """One window, right before the caller's newest turn, naming the step."""
    windows = [i for i, m in enumerate(messages) if is_marked(m, WINDOW_MARKER)]
    assert len(windows) == 1, messages
    assert messages[-1]["role"] == "user", messages
    assert windows[0] == len(messages) - 2, messages
    assert step_text in text_of(messages[windows[0]])


def _index(messages: list[dict[str, Any]], needle: str) -> int:
    return next(i for i, m in enumerate(messages) if needle in json.dumps(m))


@pytest.mark.parametrize("played", [0.1, 0.0], ids=["after-a-beat", "at-once"])
async def test_the_window_stays_before_the_callers_turn_after_a_barge_in(played):
    """However much of the cut-off reply the aggregator closed into the
    context, and whenever, the next turn's window replaces the last and sits
    right before the caller's turn — the reply's words ahead of it."""
    holder = _holder()

    def reply() -> list[Frame]:
        return [
            LLMFullResponseStartFrame(),
            LLMTextFrame("Let's look at the router."),
            LLMTextFrame(" Which lights are on?"),
        ]

    llm, _rule = await _call(
        [reply, list],
        [
            _caller("My internet is down."),
            SleepFrame(sleep=played),
            InterruptionFrame(),
            _BEAT,
            _caller("Hang on, the power light is off."),
        ],
        holder,
    )

    assert len(llm.read) == 2
    first, second = llm.read
    _assert_window_before_the_turn(first, "Ask which lights")
    _assert_window_before_the_turn(second, "Ask which lights")
    if played:
        # The words the caller heard stay in the turn they cut off.
        heard = _index(second, "Which lights are on?")
        assert second[heard]["role"] == "assistant"
        assert "Let's look at the router." in text_of(second[heard])
        assert heard < len(second) - 2


async def test_a_barge_in_on_the_callers_heels_still_gets_the_window():
    """The caller's next turn lands right behind the interruption, while the
    aggregator may still be closing the cut-off reply."""
    holder = _holder()

    def reply() -> list[Frame]:
        return [LLMFullResponseStartFrame(), LLMTextFrame("Let's look at the router.")]

    llm, _rule = await _call(
        [reply, list],
        [
            _caller("My internet is down."),
            _BEAT,
            InterruptionFrame(),
            _caller("The power light is off."),
        ],
        holder,
    )

    assert len(llm.read) == 2
    _assert_window_before_the_turn(llm.read[1], "Ask which lights")


async def test_a_move_beside_cut_off_speech_costs_no_inference():
    """A move reported beside speech asks for no inference of its own, and
    the barge-in does not change that: the caller's next turn is the next
    inference, and its window is the step moved to."""
    holder = _holder()

    def reply() -> list[Frame]:
        _moved(holder, "restart")()
        return [
            LLMFullResponseStartFrame(),
            LLMTextFrame("Then let's restart the router."),
            *_calls((MOVE_TO_STEP, "m1", True)),
            _result(MOVE_TO_STEP, "m1", {"success": True}),
        ]

    llm, rule = await _call(
        [reply, list],
        [
            _caller("The internet light is red."),
            _BEAT,
            InterruptionFrame(),
            _BEAT,
            _caller("Okay, how long do I wait?"),
        ],
        holder,
    )

    assert len(llm.read) == 2
    assert rule.unsettled == 0
    second = llm.read[1]
    _assert_window_before_the_turn(second, "restart the router and wait")
    assert _index(second, '"m1"') < len(second) - 2


async def test_a_lookup_cancelled_by_the_barge_in_holds_nothing():
    """The move settled first, held at no inference while its sibling ran;
    the barge-in cancelled the sibling. Nothing is left waiting, no extra
    inference runs, and the next turn reads both outcomes before its window."""
    holder = _holder()

    def reply() -> list[Frame]:
        _moved(holder, "restart")()
        return [
            LLMFullResponseStartFrame(),
            LLMTextFrame("One moment."),
            *_calls((MOVE_TO_STEP, "m1", True), ("line_status", "l1", True)),
            _result(MOVE_TO_STEP, "m1", {"success": True}),
        ]

    llm, rule = await _call(
        [reply, list],
        [
            _caller("The internet light is red."),
            _BEAT,
            InterruptionFrame(),
            _BEAT,
            _caller("Never mind that, what now?"),
        ],
        holder,
    )

    assert len(llm.read) == 2
    assert rule.unsettled == 0
    second = llm.read[1]
    _assert_window_before_the_turn(second, "restart the router and wait")
    assert _index(second, "CANCELLED") < len(second) - 2


async def test_an_async_lookup_answering_after_the_barge_in_is_answered():
    """An async sibling survives the barge-in; its late result is pipecat's
    rule again (the move rule forgot the response), so it runs the one
    inference that answers it — never held at none."""
    holder = _holder()

    def reply() -> list[Frame]:
        _moved(holder, "restart")()
        return [
            LLMFullResponseStartFrame(),
            LLMTextFrame("Let me check your line."),
            *_calls((MOVE_TO_STEP, "m1", True), ("line_status", "l1", False)),
            _result(MOVE_TO_STEP, "m1", {"success": True}),
        ]

    llm, rule = await _call(
        [reply, list],
        [
            _caller("The internet light is red."),
            _BEAT,
            InterruptionFrame(),
            _BEAT,
            _result("line_status", "l1", {"line": "up"}),
        ],
        holder,
    )

    assert rule.unsettled == 0
    assert len(llm.read) == 2
    assert any(
        m.get("tool_call_id") == "l1" and '"up"' in str(m.get("content"))
        for m in llm.read[1]
    )
