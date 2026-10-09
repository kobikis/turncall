"""One Smart Turn model per process, one turn per call (#271).

Every call used to load its own ONNX session of the same weights — about
36 MiB of the ~115 MiB a call costs (`docs/guides/capacity.mdx`). The session is
shared now; what a call has heard stays its own.
"""

import numpy as np
import pytest

from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import _build_turn_strategies

pytestmark = pytest.mark.unit

RATE = 16000


def _sessions_built(mocker) -> list:
    import onnxruntime

    built: list = []
    real = onnxruntime.InferenceSession

    def counting(*args, **kwargs):
        session = real(*args, **kwargs)
        built.append(session)
        return session

    mocker.patch.object(onnxruntime, "InferenceSession", counting)
    return built


def _analyzer():
    """The Smart Turn analyzer a call's turn strategies carry."""
    [stop] = _build_turn_strategies(
        AgentConfig(smart_turn_detection=True), smart_turn=True
    ).stop
    analyzer = stop._turn_analyzer
    analyzer.set_sample_rate(RATE)
    return analyzer


def _speech(seconds: float) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16).tobytes()


def test_the_shared_analyzer_sets_everything_pipecats_does() -> None:
    """`SharedSmartTurnAnalyzer` skips pipecat's constructor and sets what it
    sets by hand. A pipecat upgrade that gives that constructor something new
    to set would leave the shared one without it — fail here, naming it."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )

    from turncall.orchestrator.smart_turn import SharedSmartTurnAnalyzer

    assert set(vars(SharedSmartTurnAnalyzer())) == set(vars(LocalSmartTurnAnalyzerV3()))


def test_many_calls_load_the_model_at_most_once(mocker) -> None:
    built = _sessions_built(mocker)

    for _ in range(3):
        _analyzer()

    assert len(built) <= 1


async def test_what_one_call_heard_never_reaches_another() -> None:
    first, second = _analyzer(), _analyzer()

    first.append_audio(_speech(1.0), is_speech=True)

    assert first.speech_triggered
    assert not second.speech_triggered

    state, _ = await first.analyze_end_of_turn()
    assert not second.speech_triggered
    assert state is not None
