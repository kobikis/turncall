"""Audio resampler processor for S2S pipelines.

Resamples InputAudioRawFrame between sample rates (e.g., 16kHz WebRTC → 24kHz
OpenAI Realtime) and TTSAudioRawFrame in the reverse direction.

Uses Pipecat's stateful SOXR stream resampler, which carries filter state
across frames. (The previous audioop.ratecv version passed None as state every
frame, resetting the filter at each boundary and producing clicks/aliasing on
continuous audio — and audioop is removed in Python 3.13.)
"""

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import Frame, InputAudioRawFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class AudioResampler(FrameProcessor):
    """Resamples audio frames between pipeline sample rate and S2S service rate.

    Handles bidirectional resampling:
    - InputAudioRawFrame (downstream): pipeline_rate → service_rate
    - TTSAudioRawFrame (downstream): service_rate → pipeline_rate

    One instance handles one direction in practice (each appears once in the
    pipeline at a position where only that frame type flows), but it stays
    symmetric so a single class covers both. Each direction owns a dedicated
    stateful resampler so filter state is continuous across frames.
    """

    def __init__(
        self,
        *,
        pipeline_sample_rate: int = 8000,
        service_sample_rate: int = 24000,
    ) -> None:
        super().__init__()
        self._pipeline_rate = pipeline_sample_rate
        self._service_rate = service_sample_rate
        self._in_resampler = create_stream_resampler()  # pipeline → service
        self._out_resampler = create_stream_resampler()  # service → pipeline

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if (
            isinstance(frame, InputAudioRawFrame)
            and direction == FrameDirection.DOWNSTREAM
            and frame.sample_rate != self._service_rate
        ):
            audio = await self._in_resampler.resample(
                frame.audio, frame.sample_rate, self._service_rate
            )
            # The stream resampler buffers in its delay line and can return
            # empty early; don't emit an empty audio frame (services reject it).
            if not audio:
                return
            frame = InputAudioRawFrame(
                audio=audio,
                sample_rate=self._service_rate,
                num_channels=frame.num_channels,
            )
        elif (
            isinstance(frame, TTSAudioRawFrame)
            and direction == FrameDirection.DOWNSTREAM
            and frame.sample_rate != self._pipeline_rate
        ):
            audio = await self._out_resampler.resample(
                frame.audio, frame.sample_rate, self._pipeline_rate
            )
            if not audio:
                return
            frame = TTSAudioRawFrame(
                audio=audio,
                sample_rate=self._pipeline_rate,
                num_channels=frame.num_channels,
            )

        await self.push_frame(frame, direction)
