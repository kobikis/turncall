"""Deepgram Flux owns end-of-turn (#204).

A `flux-*` model on the `deepgram` provider is Flux: its own service on its own
endpoint, and the STT that decides when the caller's turn is over. TurnCall's
job is to let it — Smart Turn not stacked behind it, VAD not given the agent's
`silence_timeout_ms` — and to keep measuring the turn in `metrics.turn`.

The behavioural half drives the processors `create_pipeline` actually builds
for a Flux agent with the messages Deepgram's socket sends, so it fails if the
wiring between the service and the aggregator is ever dropped.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    DataFrame,
    InterruptionFrame,
    LLMContextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.tests.utils import SleepFrame, run_test

from tests.conftest import creds
from turncall.config.settings import PipecatSettings
from turncall.domain.models import AgentConfig, STTConfig
from turncall.orchestrator.pipeline_factory import (
    _build_turn_strategies,
    _build_vad_analyzer,
    _create_stt_service,
)
from turncall.orchestrator.procedures import CallProcedures
from turncall.orchestrator.turn_metrics import TurnMetricsRecorder

pytestmark = pytest.mark.unit

FLUX = "flux-general-en"


def _flux(**over: object) -> AgentConfig:
    stt = over.pop("stt", None) or STTConfig(model=FLUX)
    return AgentConfig(stt=stt, **over)  # type: ignore[arg-type]


def _service(config: AgentConfig) -> DeepgramFluxSTTService:
    return _create_stt_service(config, creds("sk-test", deepgram="dg-test"))


def _connection_url(service: DeepgramFluxSTTService) -> str:
    """What Flux is actually asked for: its settings only reach Deepgram
    through the query string, built at connect."""
    service._sample_rate = service._init_sample_rate
    return service._build_query_string()


class TestTheService:
    @pytest.mark.parametrize("model", ["flux-general-en", "flux-general-multi"])
    def test_a_flux_model_gets_the_flux_service(self, model: str) -> None:
        """Flux is served on `/v2/listen`; the v1 service would ask the wrong
        endpoint for it."""
        service = _service(_flux(stt=STTConfig(model=model)))

        assert type(service) is DeepgramFluxSTTService
        assert service._settings.model == model

    def test_nova_is_untouched(self) -> None:
        assert type(_service(AgentConfig())) is DeepgramSTTService

    def test_keyterms_go_out_as_keyterm_and_beat_a_stale_extra(self) -> None:
        service = _service(
            _flux(
                stt=STTConfig(model=FLUX, keyterms=["Acme"], extra={"keyterm": ["x"]})
            )
        )

        assert service._settings.keyterm == ["Acme"]
        assert "keyterm=Acme" in _connection_url(service)
        assert "keyterm=x" not in _connection_url(service)

    def test_flux_settings_in_extra_reach_the_connection(self) -> None:
        """Flux sends declared settings only, never its `extra` — so its own
        end-of-turn knobs must land on their fields to be heard at all."""
        service = _service(
            _flux(
                stt=STTConfig(
                    model=FLUX, extra={"eot_threshold": 0.8, "eot_timeout_ms": 3000}
                )
            )
        )

        url = _connection_url(service)
        assert "eot_threshold=0.8" in url
        assert "eot_timeout_ms=3000" in url

    def test_a_key_flux_does_not_declare_is_not_sent(self) -> None:
        """A Nova-only option left in `extra` from before the switch."""
        service = _service(
            _flux(stt=STTConfig(model=FLUX, extra={"smart_format": True}))
        )

        assert "smart_format" not in _connection_url(service)

    def test_the_transport_rate_reaches_the_connection(self) -> None:
        """Twilio is 8k; Flux takes it, and a wrong rate is garbled audio."""
        service = _create_stt_service(
            _flux(), creds("sk-test", deepgram="dg-test"), sample_rate=8000
        )

        assert "sample_rate=8000" in _connection_url(service)

    def test_an_eager_threshold_turns_the_prediction_on(self) -> None:
        """The threshold alone makes Flux send predictions; the flag is what
        has the service act on them."""
        service = _service(
            _flux(stt=STTConfig(model=FLUX, extra={"eager_eot_threshold": 0.5}))
        )

        assert service.eager_end_of_turn_enabled
        assert "eager_eot_threshold=0.5" in _connection_url(service)

    def test_no_threshold_no_prediction(self) -> None:
        assert not _service(_flux()).eager_end_of_turn_enabled

    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_service_agrees_with_the_agent_on_barge_in(self, enabled: bool) -> None:
        assert (
            _service(_flux(interruption_enabled=enabled))._should_interrupt is enabled
        )


class TestTheTurnIsFluxs:
    def test_smart_turn_is_not_stacked_behind_flux(self) -> None:
        """`smart_turn_detection` defaults to true; on a Flux agent it must not
        add a second end-of-turn wait after Flux's."""
        strategies = _build_turn_strategies(
            _flux(smart_turn_detection=True), smart_turn=True
        )

        assert [type(s).__name__ for s in strategies.start] == [
            "ExternalUserTurnStartStrategy"
        ]
        assert [type(s).__name__ for s in strategies.stop] == [
            "ExternalUserTurnStopStrategy"
        ]

    @pytest.mark.parametrize("enabled", [True, False])
    def test_barge_in_follows_the_agent(self, enabled: bool) -> None:
        strategies = _build_turn_strategies(
            _flux(interruption_enabled=enabled), smart_turn=True
        )

        assert strategies.start[0]._enable_interruptions is enabled

    def test_an_eager_agent_answers_the_prediction(self) -> None:
        strategies = _build_turn_strategies(
            _flux(stt=STTConfig(model=FLUX, extra={"eager_eot_threshold": 0.5})),
            smart_turn=True,
        )

        assert [type(s).__name__ for s in strategies.stop] == [
            "EagerUserTurnStopStrategy"
        ]

    def test_nova_keeps_smart_turn(self) -> None:
        strategies = _build_turn_strategies(AgentConfig(), smart_turn=True)

        assert type(strategies.stop[0]).__name__ == "TurnAnalyzerUserTurnStopStrategy"

    @pytest.mark.parametrize("smart_turn_detection", [True, False])
    def test_vad_keeps_pipecats_window_whatever_silence_timeout_says(
        self, smart_turn_detection: bool
    ) -> None:
        """VAD decides nothing on a Flux agent, so the agent's turn window
        would only delay the moment the latency observer measures from."""
        analyzer = _build_vad_analyzer(
            _flux(silence_timeout_ms=2000, smart_turn_detection=smart_turn_detection),
            sample_rate=8000,
            smart_turn=True,
        )

        assert analyzer._params.stop_secs == 0.2


# --- Behaviour: the processors create_pipeline builds, driven like Deepgram ---


class _Server(DataFrame):
    """A message from Deepgram's end of the Flux socket."""

    def __init__(self, event: str, transcript: str = "") -> None:
        super().__init__()
        self.message = {"type": "TurnInfo", "event": event, "transcript": transcript}


class _Silence(DataFrame):
    """The caller goes quiet: what the aggregator's VAD reports, made here
    because no audio flows in this test."""


class _Noise(DataFrame):
    """VAD hears sound on the line and reports speech."""


class _Marker(DataFrame):
    pass


class _FluxSocket(FrameProcessor):
    """Stands in for the network: plays server messages into the service."""

    def __init__(self, stt: DeepgramFluxSTTService) -> None:
        super().__init__()
        self._stt = stt

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, _Server):
            await self._stt._handle_message(frame.message)
        elif isinstance(frame, _Noise):
            await self.push_frame(
                VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time())
            )
        elif isinstance(frame, _Silence):
            await self.push_frame(
                VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=time.time())
            )
        else:
            await self.push_frame(frame, direction)


def _built_for_flux(mocker, **over: object) -> tuple[DeepgramFluxSTTService, object]:
    """The STT and user aggregator `create_pipeline` builds for a Flux agent."""
    from turncall.orchestrator import pipeline_factory

    captured: list = []
    mocker.patch.object(
        pipeline_factory,
        "Pipeline",
        lambda processors: captured.extend(processors) or mocker.MagicMock(),
    )
    pipeline_factory.create_pipeline(
        config=_flux(**over),
        transport=mocker.MagicMock(),
        call_context=SimpleNamespace(
            is_eval=False,
            call_id=uuid4(),
            session_factory=None,
            procedures=CallProcedures(),
        ),
        credentials=creds("sk-test", deepgram="dg-test"),
        pipecat_settings=PipecatSettings(),
        audio_sample_rate=16000,
    )
    stt = next(p for p in captured if isinstance(p, DeepgramFluxSTTService))
    user = next(p for p in captured if isinstance(p, LLMUserAggregator))
    # No socket: messages are played in by `_FluxSocket`.
    mocker.patch.object(stt, "_connect", AsyncMock())
    mocker.patch.object(stt, "_disconnect", AsyncMock())
    return stt, user


async def _drive(mocker, frames: list, *, observers=None, **over: object) -> list:
    stt, user = _built_for_flux(mocker, **over)
    down, _ = await run_test(
        Pipeline([_FluxSocket(stt), stt, user]),
        frames_to_send=frames,
        observers=observers or [],
    )
    return down


async def test_flux_end_of_turn_asks_the_llm(mocker) -> None:
    """The turn ends on Flux's EndOfTurn, with no other verdict waited for —
    even while VAD still hears sound on the line, since VAD decides nothing
    here. Under Smart Turn the same turn stays open until VAD hears silence.
    The LLM is asked before anything sent after the EndOfTurn arrives."""
    down = await _drive(
        mocker,
        [
            _Noise(),
            _Server("StartOfTurn"),
            _Server("EndOfTurn", "Table for two at seven."),
            SleepFrame(0.3),
            _Marker(),
        ],
    )

    kinds = [type(f) for f in down]
    assert LLMContextFrame in kinds, "Flux's end of turn never reached the LLM"
    assert kinds.index(LLMContextFrame) < kinds.index(_Marker)
    context = next(f for f in down if isinstance(f, LLMContextFrame)).context
    assert "Table for two at seven." in str(context.get_messages())


@pytest.mark.parametrize("enabled", [True, False])
async def test_flux_start_of_turn_barges_in_only_when_allowed(mocker, enabled) -> None:
    down = await _drive(
        mocker,
        [_Server("StartOfTurn"), SleepFrame(0.2)],
        interruption_enabled=enabled,
    )

    assert any(isinstance(f, InterruptionFrame) for f in down) is enabled


async def test_metrics_turn_records_the_turn_end_flux_decided(mocker) -> None:
    """From the caller's silence to Flux's EndOfTurn: 0.2s of VAD window plus
    the 0.3s Flux took, here."""
    written: list[dict] = []

    async def write(payload: dict) -> None:
        written.append(payload)

    recorder = TurnMetricsRecorder(SimpleNamespace(is_eval=False), write=write)
    await _drive(
        mocker,
        [
            _Server("StartOfTurn"),
            _Silence(),
            SleepFrame(0.3),
            _Server("EndOfTurn", "Table for two at seven."),
            SleepFrame(0.2),
            BotStartedSpeakingFrame(),
            SleepFrame(0.1),
        ],
        observers=[recorder.observer],
    )

    [turn] = written
    # 500 ms of sleeps between the frames; the clock reads each frame as it is
    # observed, so a loaded run can come in a few ms under it.
    assert 450 <= turn["turn_end_ms"] < 1500
    assert turn["user_to_bot_ms"] >= turn["turn_end_ms"]


async def test_an_eager_end_of_turn_asks_the_llm_once(mocker) -> None:
    """The prediction starts the reply while the turn is open; an EndOfTurn
    that agrees with it releases that reply instead of asking again."""
    down = await _drive(
        mocker,
        [
            _Noise(),
            _Server("StartOfTurn"),
            _Server("EagerEndOfTurn", "Table for two at seven"),
            SleepFrame(0.2),
            _Marker(),
            _Server("EndOfTurn", "Table for two at seven."),
            SleepFrame(0.2),
        ],
        stt=STTConfig(model=FLUX, extra={"eager_eot_threshold": 0.5}),
    )

    kinds = [type(f) for f in down]
    asked = [f for f in down if isinstance(f, LLMContextFrame)]
    assert len(asked) == 1, "the confirmed turn was answered twice"
    assert asked[0].speculation
    assert kinds.index(LLMContextFrame) < kinds.index(_Marker)
