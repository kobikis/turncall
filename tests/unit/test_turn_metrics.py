"""Turn metrics: frames in, one `metrics.turn` record per turn out (#176).

Drives pipecat's own latency observer with the frames a real turn produces and
checks what the recorder hands to the store — so a change in pipecat's event
contract fails here rather than silently leaving calls without metrics.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    ClientConnectedFrame,
    MetricsFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from turncall.orchestrator.turn_metrics import (
    TurnMetricsRecorder,
    turn_metrics_observers,
)

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _push(observer, frame):
    source = SimpleNamespace(name="src")
    return observer.on_push_frame(
        FramePushed(
            source=source,
            destination=source,
            frame=frame,
            direction=FrameDirection.DOWNSTREAM,
            timestamp=0,
        )
    )


def _ttfb(*pairs):
    return MetricsFrame(
        data=[TTFBMetricsData(processor=p, value=v, model="m") for p, v in pairs]
    )


def _recorder(clock):
    written: list[dict] = []

    async def write(payload: dict) -> None:
        written.append(payload)

    ctx = SimpleNamespace(is_eval=False, call_id=uuid4())
    return TurnMetricsRecorder(ctx, write=write, time_source=clock), written


async def _turn(observer, clock, ttfbs, *, user_to_bot=0.9):
    stopped = clock.t
    await _push(observer, VADUserStoppedSpeakingFrame(stop_secs=0.0, timestamp=stopped))
    await _push(observer, _ttfb(*ttfbs))
    clock.t = stopped + user_to_bot
    await _push(observer, BotStartedSpeakingFrame())
    await _settle()


async def _settle():
    # Event handlers and the write both run as tasks, off the frame path.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_cascade_turn_records_each_stage():
    clock = _Clock()
    rec, written = _recorder(clock)
    await _turn(
        rec.observer,
        clock,
        [
            ("DeepgramSTTService#0", 0.12),
            ("OpenAILLMService#0", 0.34),
            ("CartesiaTTSService#0", 0.21),
        ],
    )
    [turn] = written
    assert turn["turn"] == 1
    assert turn["user_to_bot_ms"] == 900
    assert turn["stages"] == {
        "stt": {"ttfb_ms": 120, "service": "DeepgramSTTService#0"},
        "llm": {"ttfb_ms": 340, "service": "OpenAILLMService#0"},
        "tts": {"ttfb_ms": 210, "service": "CartesiaTTSService#0"},
    }


async def test_s2s_turn_omits_stages_it_does_not_have():
    clock = _Clock()
    rec, written = _recorder(clock)
    await _turn(rec.observer, clock, [("GeminiLiveLLMService#0", 0.4)])
    assert written[0]["stages"] == {
        "llm": {"ttfb_ms": 400, "service": "GeminiLiveLLMService#0"}
    }


async def test_turns_are_numbered_and_the_first_stage_report_wins():
    clock = _Clock()
    rec, written = _recorder(clock)
    await _turn(rec.observer, clock, [("OpenAILLMService#0", 0.3)])
    clock.t += 5
    # A tool call makes the LLM report twice; the first is the one the caller waited on.
    await _turn(
        rec.observer, clock, [("OpenAILLMService#0", 0.5), ("OpenAILLMService#0", 0.2)]
    )
    assert [t["turn"] for t in written] == [1, 2]
    assert written[1]["stages"]["llm"]["ttfb_ms"] == 500


async def test_greeting_is_not_a_turn():
    clock = _Clock()
    rec, written = _recorder(clock)
    await _push(rec.observer, ClientConnectedFrame())
    clock.t += 1.5
    await _push(rec.observer, BotStartedSpeakingFrame())
    await _settle()
    assert written == []


def test_eval_attaches_no_recorder():
    ctx = SimpleNamespace(is_eval=True, call_id=uuid4())
    assert turn_metrics_observers(ctx) == []


def test_live_call_attaches_one():
    ctx = SimpleNamespace(is_eval=False, call_id=uuid4())
    assert len(turn_metrics_observers(ctx)) == 1
