"""WAV encoding for app-side call recordings.

The recorder hands object storage a WAV, not raw PCM. This checks the header is
valid and the samples round-trip unchanged.
"""

import wave

from turncall.orchestrator.call_recorder import _pcm16_to_wav


def test_pcm16_to_wav_roundtrips() -> None:
    # 100ms of 8kHz mono PCM16 (silence is fine; we check structure + bytes).
    sample_rate, num_channels = 8000, 1
    pcm = b"\x01\x02" * (sample_rate // 10)  # 800 samples, 2 bytes each

    wav = _pcm16_to_wav(pcm, sample_rate, num_channels)

    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"

    import io

    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == sample_rate
        assert wf.getnchannels() == num_channels
        assert wf.getsampwidth() == 2  # PCM16
        assert wf.readframes(wf.getnframes()) == pcm  # samples unchanged


if __name__ == "__main__":
    test_pcm16_to_wav_roundtrips()
    print("ok")
