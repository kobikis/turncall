"""When the caller stops speaking, on a call whose turns another party decides.

pipecat's latency observer starts a turn when VAD reports the caller stopped
and ends it when the agent starts speaking. A speech-to-speech service on
`server_vad` decides turns on its own servers, so TurnCall built no VAD there,
the observer never started a turn, and no S2S call recorded `metrics.turn`.

`CallerSpeechTimer` runs Silero for the observer alone, the way Deepgram Flux
keeps VAD "deciding nothing" (#204). It sits past the user aggregator and
pushes its VAD frames downstream only, so the aggregator — where turns start,
interruptions are broadcast and the idle guard runs — never sees them: they can
time a turn and do nothing else. The realtime services ignore VAD frames.
"""

from __future__ import annotations

from typing import Any

from pipecat.audio.vad.vad_controller import VADController
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)


class CallerSpeechTimer(FrameProcessor):
    """Reports the caller starting and stopping, downstream, for the latency
    observer; passes every frame through unchanged."""

    def __init__(self, vad_analyzer: Any) -> None:
        super().__init__()
        self._analyzer = vad_analyzer
        self._vad = VADController(vad_analyzer)
        self._vad.add_event_handler("on_speech_started", self._on_started)
        self._vad.add_event_handler("on_speech_stopped", self._on_stopped)

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        await self._vad.setup(setup)

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._vad.cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Measured before the audio goes on, so the observer has the caller's
        # stop before anything the service does with this frame.
        if (
            isinstance(frame, InputAudioRawFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._vad.process_frame(frame)
        await self.push_frame(frame, direction)

    async def _on_started(self, _controller: Any) -> None:
        await self.push_frame(
            VADUserStartedSpeakingFrame(start_secs=self._analyzer.params.start_secs)
        )

    async def _on_stopped(self, _controller: Any) -> None:
        await self.push_frame(
            VADUserStoppedSpeakingFrame(stop_secs=self._analyzer.params.stop_secs)
        )


def caller_speech_timer(
    config: Any, call_context: Any, *, sample_rate: int, pipecat_settings: Any
) -> list[Any]:
    """The timer for an S2S call on `server_vad` that records its turns.

    None on `pipecat_vad`, where the user aggregator runs Silero and broadcasts
    VAD frames itself (a second VAD would report every turn twice), and none on
    a call that records nothing (an eval, the demo), which stores no turn.
    """
    if config.s2s.turn_detection == "pipecat_vad" or not call_context.records:
        return []
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VAD_STOP_SECS, VADParams

    # pipecat's own stop window: it is subtracted from the moment VAD reports
    # the stop, so it changes when the stop is confirmed, not the measurement.
    params: dict[str, Any] = {"stop_secs": VAD_STOP_SECS}
    confidence = getattr(pipecat_settings, "vad_confidence_threshold", None)
    if confidence is not None:
        params["confidence"] = confidence
    analyzer = SileroVADAnalyzer(sample_rate=sample_rate, params=VADParams(**params))
    return [CallerSpeechTimer(analyzer)]
