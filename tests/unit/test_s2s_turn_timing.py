"""Turn latency on speech-to-speech calls.

A realtime service decides turns on its own servers (`server_vad`, the default),
so TurnCall built no VAD on those calls, and pipecat's latency observer — which
starts a turn when VAD hears the caller stop — never measured one: every S2S
call read "no latency measured". `CallerSpeechTimer` runs VAD for the observer
alone, the way Deepgram Flux keeps VAD "deciding nothing" (#204). Its frames go
downstream only: the user aggregator, upstream of it, never sees them, so they
can neither start a turn nor interrupt the agent.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import LLMService
from pipecat.tests.utils import SleepFrame, run_test

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator import pipeline_factory
from turncall.orchestrator.caller_speech_timer import CallerSpeechTimer
from turncall.orchestrator.pipeline_factory import CallContext
from turncall.orchestrator.turn_metrics import TurnMetricsRecorder

pytestmark = pytest.mark.unit

S2S_PROVIDERS = ["openai", "openai_live", "google", "aws", "xai"]
CREDS = creds("k", google="k", xai="k")


class _ScriptedVAD:
    """A VAD analyzer that hears what it is told to, one frame at a time."""

    def __init__(self, states: list[VADState]):
        self._states = iter(states)
        self.params = VADParams(start_secs=0.2, stop_secs=0.2)

    def set_sample_rate(self, sample_rate: int) -> None:
        pass

    async def analyze_audio(self, buffer: bytes) -> VADState:
        return next(self._states, VADState.QUIET)

    async def cleanup(self) -> None:
        pass


def _audio() -> InputAudioRawFrame:
    return InputAudioRawFrame(
        audio=b"\x00\x00" * 160, sample_rate=16000, num_channels=1
    )


async def test_it_reports_the_caller_starting_and_stopping_downstream_only() -> None:
    timer = CallerSpeechTimer(_ScriptedVAD([VADState.SPEAKING, VADState.QUIET]))
    down, _ = await run_test(
        timer,
        frames_to_send=[_audio(), _audio()],
        expected_down_frames=[
            VADUserStartedSpeakingFrame,
            InputAudioRawFrame,
            VADUserStoppedSpeakingFrame,
            InputAudioRawFrame,
        ],
        expected_up_frames=[],
    )
    stopped = next(f for f in down if isinstance(f, VADUserStoppedSpeakingFrame))
    assert stopped.stop_secs == 0.2


async def test_a_caller_who_mutes_mid_word_has_stopped() -> None:
    # No audio while speaking (a muted mic): pipecat's VAD controller forces
    # the stop once it is armed, so the turn is not left open until audio
    # comes back.
    timer = CallerSpeechTimer(_ScriptedVAD([VADState.SPEAKING]), audio_idle_timeout=0.1)
    await run_test(
        timer,
        frames_to_send=[_audio(), SleepFrame(0.4)],
        expected_down_frames=[
            VADUserStartedSpeakingFrame,
            InputAudioRawFrame,
            VADUserStoppedSpeakingFrame,
        ],
    )


async def test_the_user_aggregator_never_acts_on_the_timers_frames() -> None:
    # As in the S2S pipeline: the aggregator, then the timer. The aggregator is
    # where a turn starts and an interruption is broadcast; the caller's speech
    # reaching it as VAD frames would do both.
    pair = LLMContextAggregatorPair(
        context=LLMContext(),
        user_params=LLMUserAggregatorParams(user_idle_timeout=10),
        realtime_service_mode=True,
    )
    timer = CallerSpeechTimer(_ScriptedVAD([VADState.SPEAKING, VADState.QUIET]))
    down, up = await run_test(
        Pipeline([pair.user(), timer]),
        frames_to_send=[_audio(), _audio(), SleepFrame(0.1)],
    )
    seen = [type(f) for f in [*down, *up]]
    assert VADUserStoppedSpeakingFrame in seen
    assert UserStartedSpeakingFrame not in seen
    assert InterruptionFrame not in seen


async def test_an_s2s_turn_is_measured() -> None:
    written: list[dict[str, Any]] = []

    async def write(payload: dict[str, Any]) -> None:
        written.append(payload)

    ctx = SimpleNamespace(is_eval=False, records=True, call_id=uuid.uuid4())
    recorder = TurnMetricsRecorder(ctx, write=write)
    await run_test(
        CallerSpeechTimer(_ScriptedVAD([VADState.SPEAKING, VADState.QUIET])),
        # A system frame overtakes queued audio: the agent speaks after the
        # caller's stop has gone through.
        frames_to_send=[
            _audio(),
            _audio(),
            SleepFrame(0.1),
            BotStartedSpeakingFrame(),
            SleepFrame(0.1),
        ],
        observers=[recorder.observer],
    )
    assert [t["turn"] for t in written] == [1]
    assert written[0]["user_to_bot_ms"] >= 0


def _processors(mocker: Any, config: AgentConfig, *, records: bool = True) -> list[Any]:
    captured: list[Any] = []

    def _capture(processors: list[Any]) -> Any:
        captured.extend(processors)
        return mocker.MagicMock()

    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    pipeline_factory.create_pipeline(
        config=config,
        transport=mocker.MagicMock(),
        call_context=CallContext(
            call_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            call_sid="webrtc",
            stream_sid="webrtc",
            session_factory=mocker.MagicMock() if records else None,
        ),
        credentials=CREDS,
        pipecat_settings=mocker.MagicMock(vad_confidence_threshold=None),
        audio_sample_rate=16000,
    )
    return captured


def _agent(provider: str, **s2s: Any) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": provider, **s2s},
            "transport": "webrtc",
            # Nova Sonic resolves its credentials at build.
            "aws": {"access_key_id": "AKIA", "secret_access_key": "s"},
        }
    )


def _one(processors: list[Any], kind: type) -> int:
    found = [i for i, p in enumerate(processors) if isinstance(p, kind)]
    assert len(found) == 1, f"expected one {kind.__name__}, found {len(found)}"
    return found[0]


@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_server_vad_calls_time_the_caller_past_the_user_aggregator(
    mocker, provider
) -> None:
    processors = _processors(mocker, _agent(provider))
    user = _one(processors, LLMUserAggregator)
    timer = _one(processors, CallerSpeechTimer)
    service = _one(processors, LLMService)
    assert user < timer < service


async def test_pipecat_vad_calls_already_have_vad_frames(mocker) -> None:
    # The user aggregator runs Silero itself there and broadcasts VAD frames;
    # a second VAD would report every turn twice.
    processors = _processors(mocker, _agent("openai", turn_detection="pipecat_vad"))
    assert not any(isinstance(p, CallerSpeechTimer) for p in processors)


async def test_a_call_that_records_nothing_runs_no_timer(mocker) -> None:
    # An eval, or the website demo with no database: nothing would store the turn.
    processors = _processors(mocker, _agent("openai"), records=False)
    assert not any(isinstance(p, CallerSpeechTimer) for p in processors)
