"""`smart_turn_detection: false` turns Smart Turn off (#269).

pipecat 1.12's default stop strategy *is* Smart Turn V3, so an agent that asked
for it off and was handed pipecat's defaults still waited on the model, after
VAD's own `silence_timeout_ms` window: the two waits stacked. These build the
pipeline the way a call does and drive its user aggregator like VAD and the
STT would.
"""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pipecat.frames.frames import (
    DataFrame,
    LLMContextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
from pipecat.tests.utils import SleepFrame, run_test

from tests.conftest import creds
from turncall.config.settings import PipecatSettings
from turncall.domain.models import AgentConfig

pytestmark = pytest.mark.unit


class _Marker(DataFrame):
    pass


def _user_aggregator(mocker, build, **kwargs) -> tuple[object, list]:
    """The user aggregator a pipeline builder builds, and every Smart Turn
    analyzer built along the way — pipecat's default or TurnCall's."""
    from pipecat.audio.turn.smart_turn import base_smart_turn

    from turncall.orchestrator import pipeline_factory

    models: list = []
    real_init = base_smart_turn.BaseSmartTurn.__init__

    def spy(self, *args, **kwargs):
        models.append(self)
        real_init(self, *args, **kwargs)

    mocker.patch.object(base_smart_turn.BaseSmartTurn, "__init__", spy)
    captured: list = []
    mocker.patch.object(
        pipeline_factory,
        "Pipeline",
        lambda processors: captured.extend(processors) or mocker.MagicMock(),
    )
    getattr(pipeline_factory, build)(
        transport=mocker.MagicMock(),
        pipecat_settings=PipecatSettings(),
        audio_sample_rate=16000,
        **kwargs,
    )
    user = next(p for p in captured if isinstance(p, LLMUserAggregator))
    return user, models


def _built_user_aggregator(mocker, config: AgentConfig) -> tuple[object, list]:
    return _user_aggregator(
        mocker,
        "create_pipeline",
        config=config,
        call_context=SimpleNamespace(
            is_eval=False, call_id=uuid4(), session_factory=None
        ),
        credentials=creds("sk-test", deepgram="dg-test"),
    )


def _a_turn(stop_secs: float) -> list:
    """The caller says one sentence and goes quiet; VAD reports the silence
    once its own window has passed."""
    return [
        VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time()),
        TranscriptionFrame("Table for two at seven.", "caller", "now", finalized=True),
        VADUserStoppedSpeakingFrame(stop_secs=stop_secs, timestamp=time.time()),
        SleepFrame(0.5),
        _Marker(),
    ]


@pytest.mark.parametrize("barge_in", [True, False])
async def test_smart_turn_off_builds_no_smart_turn_model(mocker, barge_in) -> None:
    _, models = _built_user_aggregator(
        mocker,
        AgentConfig(smart_turn_detection=False, interruption_enabled=barge_in),
    )

    assert models == []


@pytest.mark.parametrize("barge_in", [True, False])
async def test_smart_turn_off_ends_the_turn_on_vads_silence(mocker, barge_in) -> None:
    """VAD has already waited out `silence_timeout_ms`; nothing waits after it,
    so the LLM is asked well inside half a second."""
    config = AgentConfig(smart_turn_detection=False, interruption_enabled=barge_in)
    user, _ = _built_user_aggregator(mocker, config)

    down, _ = await run_test(
        Pipeline([user]), frames_to_send=_a_turn(config.silence_timeout_ms / 1000)
    )

    kinds = [type(f) for f in down]
    assert LLMContextFrame in kinds, "the turn never reached the LLM"
    assert kinds.index(LLMContextFrame) < kinds.index(_Marker)


def _s2s_user_aggregator(mocker, config: AgentConfig) -> tuple[object, list]:
    return _user_aggregator(
        mocker,
        "_create_s2s_pipeline",
        config=config,
        call_context=mocker.MagicMock(),
        credentials=creds("sk-test"),
    )


async def test_s2s_on_pipecat_vad_builds_no_smart_turn_model(mocker) -> None:
    """S2S has no Smart Turn: `silence_timeout_ms` alone ends the turn there.
    Left to pipecat's defaults it built the model anyway and stacked it on."""
    from turncall.domain.models import S2SConfig

    config = AgentConfig(
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="openai", turn_detection="pipecat_vad"),
    )
    _, models = _s2s_user_aggregator(mocker, config)

    assert models == []


async def test_smart_turn_on_still_builds_one_analyzer_per_call(mocker) -> None:
    """One per call — on top of the process's one model owner (#271), built
    the first time any call needs Smart Turn, so warmed here first."""
    from turncall.orchestrator.turn_detection import _model_owner

    _model_owner()
    _, models = _built_user_aggregator(mocker, AgentConfig(smart_turn_detection=True))

    assert len(models) == 1
