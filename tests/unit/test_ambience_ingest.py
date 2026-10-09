"""Ambience ingest (#167, ADR-0021): an upload becomes one mono rendition per
transport rate, or is refused with something the uploader can act on.

The mixer's own failures are all silent — a wrong rate plays nothing, stereo
plays at double speed — so what these assert is the normalization that keeps
those states unreachable, not the audio a mixer later makes of it.
"""

import io
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from turncall.domain.carriers import CARRIERS
from turncall.orchestrator.transport_factory import (
    VOICE_OUTPUT_RATES,
    CarrierStream,
    create_carrier_transport,
    create_webrtc_transport,
    create_whatsapp_transport,
)
from turncall.services.ambience_ingest import (
    MAX_DURATION_SECONDS,
    AudioTooLongError,
    UnreadableAudioError,
    ingest_sound,
)

pytestmark = pytest.mark.unit


def _wav(seconds: float, *, rate: int = 48000, channels: int = 1) -> bytes:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    data = np.stack([tone] * channels, axis=1) if channels > 1 else tone
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="WAV")
    return buf.getvalue()


class TestRenditions:
    def test_a_rendition_exists_for_every_voice_rate(self) -> None:
        sound = ingest_sound(_wav(1), "audio/wav", VOICE_OUTPUT_RATES)
        assert set(sound.renditions) == set(VOICE_OUTPUT_RATES)

    def test_each_rendition_decodes_at_the_rate_it_claims_and_is_mono(self) -> None:
        sound = ingest_sound(_wav(2, channels=2), "audio/wav", VOICE_OUTPUT_RATES)
        for rate, data in sound.renditions.items():
            info = sf.info(io.BytesIO(data))
            assert info.samplerate == rate
            assert info.channels == 1
            assert info.format == "MP3"

    def test_stereo_is_forced_to_mono_not_read_as_interleaved(self) -> None:
        """Stereo read as mono doubles the sample count — the double-speed bug."""
        sound = ingest_sound(_wav(2, channels=2), "audio/wav", [16000])
        frames = sf.info(io.BytesIO(sound.renditions[16000])).frames
        assert frames == pytest.approx(2 * 16000, rel=0.05)

    def test_duration_is_recorded(self) -> None:
        assert ingest_sound(_wav(3), "audio/wav", [8000]).duration_ms == 3000


class TestPhoneBandWarning:
    """A sound can upload, play and be inaudible on a phone line — measured on a
    real call. The upload says so rather than leaving the caller to find out."""

    def _rumble(self) -> bytes:
        t = np.linspace(0, 2, 2 * 48000, endpoint=False)
        buf = io.BytesIO()
        sf.write(buf, 0.5 * np.sin(2 * np.pi * 60 * t), 48000, format="WAV")
        return buf.getvalue()

    def test_a_low_rumble_is_warned_about(self) -> None:
        sound = ingest_sound(self._rumble(), "audio/wav", VOICE_OUTPUT_RATES)
        assert [w["code"] for w in sound.warnings] == ["inaudible_on_phone"]
        assert sound.renditions  # advisory: still stored

    def test_a_sound_in_the_phone_band_is_not(self) -> None:
        assert ingest_sound(_wav(1), "audio/wav", VOICE_OUTPUT_RATES).warnings == []


class TestRefusals:
    def test_over_the_duration_cap(self) -> None:
        with pytest.raises(AudioTooLongError, match=f"{MAX_DURATION_SECONDS}s"):
            ingest_sound(_wav(MAX_DURATION_SECONDS + 1, rate=8000), "audio/wav", [8000])

    def test_an_unreadable_file_names_the_formats_and_the_fix(self) -> None:
        """An .m4a sent as audio/mpeg passes the header check and fails here."""
        with pytest.raises(UnreadableAudioError) as exc:
            ingest_sound(b"\x00\x00\x00\x20ftypM4A " * 50, "audio/mpeg", [8000])
        assert "WAV" in str(exc.value)
        assert "ffmpeg" in str(exc.value)

    def test_a_non_audio_content_type(self) -> None:
        with pytest.raises(UnreadableAudioError, match="application/pdf"):
            ingest_sound(_wav(1), "application/pdf", [8000])


class TestEveryVoiceTransportRateHasARendition:
    """The guard that catches a transport added later at a rate nothing
    renders for — whose calls would then silently have no ambience."""

    @pytest.mark.parametrize(
        "build",
        [
            *(
                lambda c=c: create_carrier_transport(
                    c, MagicMock(), CarrierStream("MZ1")
                )
                for c in CARRIERS
            ),
            lambda: create_webrtc_transport()[0],
            lambda: create_whatsapp_transport(MagicMock()),
        ],
        ids=[*CARRIERS, "webrtc", "whatsapp"],
    )
    def test_output_rate_is_rendered(self, build) -> None:
        assert build()._params.audio_out_sample_rate in VOICE_OUTPUT_RATES
