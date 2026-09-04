"""Checks AudioResampler routes frames to the right rate.

The old audioop version reset filter state every frame (clicks/aliasing) and
audioop is gone in 3.13. This guards the SOXR-based replacement: correct output
rate per direction, and untouched passthrough when the rate already matches.
"""

import asyncio

from pipecat.frames.frames import InputAudioRawFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from turncall.orchestrator.audio_resampler import AudioResampler


def _run(coro):
    return asyncio.run(coro)


def test_resampler_routing() -> None:
    async def main() -> None:
        r = AudioResampler(pipeline_sample_rate=16000, service_sample_rate=24000)
        out: list = []

        async def capture(frame, direction):  # push_frame is awaited
            out.append(frame)

        r.push_frame = capture  # type: ignore[assignment]

        # mic input 16k -> service 24k
        await r.process_frame(
            InputAudioRawFrame(audio=b"\x00\x00" * 1600, sample_rate=16000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        assert isinstance(out[-1], InputAudioRawFrame)
        assert out[-1].sample_rate == 24000

        # service TTS 24k -> pipeline 16k
        await r.process_frame(
            TTSAudioRawFrame(audio=b"\x00\x00" * 2400, sample_rate=24000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        assert isinstance(out[-1], TTSAudioRawFrame)
        assert out[-1].sample_rate == 16000

        # already-correct rate passes through the same object untouched
        f = InputAudioRawFrame(audio=b"\x00\x00" * 2400, sample_rate=24000, num_channels=1)
        await r.process_frame(f, FrameDirection.DOWNSTREAM)
        assert out[-1] is f

    _run(main())


def test_empty_resample_drops_frame() -> None:
    # SOXR buffers in its delay line and can return empty early; an empty audio
    # frame makes realtime services error ("got empty bytes"), so it must not push.
    async def main() -> None:
        r = AudioResampler(pipeline_sample_rate=16000, service_sample_rate=24000)
        out: list = []

        async def capture(frame, direction):
            out.append(frame)

        async def empty(audio, in_rate, out_rate):
            return b""

        r.push_frame = capture  # type: ignore[assignment]
        r._in_resampler.resample = empty  # type: ignore[assignment]

        await r.process_frame(
            InputAudioRawFrame(audio=b"\x00\x00" * 1600, sample_rate=16000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        assert out == []  # nothing emitted; samples stay buffered for next call

    _run(main())


if __name__ == "__main__":
    test_resampler_routing()
    test_empty_resample_drops_frame()
    print("OK")
