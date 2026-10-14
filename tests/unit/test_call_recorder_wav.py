"""WAV encoding for app-side call recordings.

The recorder hands object storage a WAV, not raw PCM. This checks the header is
valid and the samples round-trip unchanged.
"""

import io
import wave

import pytest

from turncall.orchestrator.call_recorder import _pcm16_to_wav

pytestmark = pytest.mark.unit


def test_pcm16_to_wav_roundtrips() -> None:
    # 100ms of 8kHz mono PCM16 (silence is fine; we check structure + bytes).
    sample_rate, num_channels = 8000, 1
    pcm = b"\x01\x02" * (sample_rate // 10)  # 800 samples, 2 bytes each

    wav = _pcm16_to_wav(pcm, sample_rate, num_channels)

    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"

    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == sample_rate
        assert wf.getnchannels() == num_channels
        assert wf.getsampwidth() == 2  # PCM16
        assert wf.readframes(wf.getnframes()) == pcm  # samples unchanged


if __name__ == "__main__":
    test_pcm16_to_wav_roundtrips()
    print("ok")


async def test_recording_is_stereo_caller_left_agent_right() -> None:
    """Frames in, WAV out (#178): each speaker on their own channel."""
    import struct
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
    from pipecat.tests.utils import run_test

    from turncall.orchestrator.call_recorder import CallRecorder

    rate = 8000
    n = rate // 10  # 100ms each
    caller = struct.pack(f"<{n}h", *([1000] * n))
    agent = struct.pack(f"<{n}h", *([-2000] * n))

    uploaded: dict[str, bytes] = {}

    async def upload(key, data, content_type):
        uploaded[key] = data
        return f"file:///{key}"

    storage = SimpleNamespace(upload=upload)
    ctx = SimpleNamespace(
        is_eval=False,
        records=True,
        has_database=True,
        call_id=uuid4(),
        project_id=uuid4(),
    )
    recorder = CallRecorder(ctx, sample_rate=rate)
    with (
        patch(
            "turncall.orchestrator.call_recorder.create_storage_adapter",
            return_value=storage,
        ),
        patch.object(CallRecorder, "_persist", AsyncMock()),
        patch.object(CallRecorder, "_set_status", AsyncMock()),
    ):
        await run_test(
            recorder,
            frames_to_send=[
                InputAudioRawFrame(audio=caller, sample_rate=rate, num_channels=1),
                OutputAudioRawFrame(audio=agent, sample_rate=rate, num_channels=1),
            ],
            ignore_start=False,
            expected_down_frames=None,
        )

    [wav] = uploaded.values()
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 2
        frames = wf.readframes(wf.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    left, right = samples[0::2], samples[1::2]
    assert 1000 in left and 1000 not in right
    assert -2000 in right and -2000 not in left


async def test_a_call_with_no_audio_still_ends_its_recording() -> None:
    """pipecat skips on_audio_data when nothing was buffered — a call whose
    providers refused before anyone spoke. The status has to reach a terminal
    value anyway, or call.ended waits out its whole timeout for it (#189)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from pipecat.tests.utils import run_test

    from turncall.domain.enums import RecordingStatus
    from turncall.orchestrator.call_recorder import CallRecorder

    ctx = SimpleNamespace(
        is_eval=False,
        records=True,
        has_database=True,
        call_id=uuid4(),
        project_id=uuid4(),
    )
    recorder = CallRecorder(ctx, sample_rate=8000)
    with patch.object(CallRecorder, "_set_status", AsyncMock()) as set_status:
        await run_test(
            recorder, frames_to_send=[], ignore_start=False, expected_down_frames=None
        )

    statuses = [c.args[0] for c in set_status.await_args_list]
    assert statuses == [RecordingStatus.IN_PROGRESS, RecordingStatus.FAILED]
