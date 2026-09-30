"""Ambience sound ingest: one upload in, one mono MP3 rendition per rate out.

ADR-0021. pipecat's `SoundfileMixer` fails silently on anything but mono at
the transport's exact rate — a wrong rate mixes nothing, stereo plays at double
speed — so every sound is normalized here, once, rather than validated. Bytes
in, bytes out: no HTTP, no session, no storage adapter.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
import soundfile as sf
import soxr

# The mixer holds the whole file in memory per call: 60s @ 16kHz is 1.92 MB a
# call, 192 MB across 100 concurrent calls. Ambience loops, so nobody needs more.
MAX_DURATION_SECONDS = 60

ACCEPTED_FORMATS = "WAV, MP3, FLAC, OGG, AIFF"
_CONVERSION_HINT = (
    "An iPhone voice memo (.m4a) is AAC, which cannot be read — convert it "
    "first, e.g. `ffmpeg -i memo.m4a memo.wav`."
)


class UnreadableAudioError(ValueError):
    """The upload is not audio this platform can decode."""


class AudioTooLongError(ValueError):
    """The upload decodes, but runs past MAX_DURATION_SECONDS."""


# What a phone call actually carries (G.711 and the handsets either side).
PHONE_BAND_HZ = (300.0, 3400.0)
# Below this in-band level, before `volume`, a sound uploads and plays and
# nobody on a phone call hears it: measured on a real call, a synthetic rumble at
# -57 dBFS in-band was silent where a restaurant recording at -29 was clear.
# ponytail: one threshold from two data points; #171's real-call work can move it.
MIN_PHONE_BAND_DBFS = -50.0


@dataclass(frozen=True)
class IngestedSound:
    duration_ms: int
    # rate (Hz) -> mono MP3 bytes at that rate.
    renditions: dict[int, bytes]
    # Advisory only ({code, message}): the upload succeeded either way.
    warnings: list[dict[str, str]] = field(default_factory=list)


def phone_band_dbfs(mono: np.ndarray, rate: int) -> float:
    """RMS level of `mono` inside PHONE_BAND_HZ, in dBFS."""
    spectrum = np.fft.rfft(mono)
    freqs = np.fft.rfftfreq(len(mono), 1 / rate)
    low, high = PHONE_BAND_HZ
    in_band = np.fft.irfft(
        np.where((freqs >= low) & (freqs <= high), spectrum, 0), len(mono)
    )
    rms = float(np.sqrt(np.mean(in_band**2)))
    return 20 * float(np.log10(rms)) if rms > 0 else float("-inf")


def _phone_band_warnings(mono: np.ndarray, rate: int) -> list[dict[str, str]]:
    level = phone_band_dbfs(mono, rate)
    if level >= MIN_PHONE_BAND_DBFS:
        return []
    return [
        {
            "code": "inaudible_on_phone",
            "message": (
                f"Almost none of this sound is in the {PHONE_BAND_HZ[0]:.0f}-"
                f"{PHONE_BAND_HZ[1]:.0f} Hz band a phone call carries (it plays at "
                f"{level:.0f} dBFS there), so a caller on a phone will hear "
                "little or nothing. Browser calls carry more of it. A recording "
                "of a real room — voices, dishes — works; a low rumble does not."
            ),
        }
    ]


def _unreadable(detail: str) -> UnreadableAudioError:
    return UnreadableAudioError(
        f"{detail}. Accepted formats: {ACCEPTED_FORMATS}. {_CONVERSION_HINT}"
    )


def ingest_sound(data: bytes, content_type: str, rates: Iterable[int]) -> IngestedSound:
    """Decode, force mono, and render one MP3 per rate.

    A content type is checked only for being plausibly audio: a renamed file
    passes any header check and fails at decode, which is the authoritative
    test and raises the same error.
    """
    if not (
        content_type.startswith("audio/") or content_type == "application/octet-stream"
    ):
        raise _unreadable(f"Unsupported file type: {content_type}")
    try:
        info = sf.info(io.BytesIO(data))
        # Refused before the full decode — the header already knows the length.
        if info.duration > MAX_DURATION_SECONDS:
            raise AudioTooLongError(
                f"Sound is {info.duration:.1f}s long. Maximum: "
                f"{MAX_DURATION_SECONDS}s — ambience loops, so a shorter "
                "recording plays for the whole call."
            )
        samples, source_rate = sf.read(
            io.BytesIO(data), dtype="float32", always_2d=True
        )
    except sf.SoundFileError as exc:
        raise _unreadable("Could not decode the file") from exc
    if samples.shape[0] == 0:
        raise _unreadable("The file contains no audio")

    # Stereo read as mono is interleaved: double speed, channels alternating.
    mono = samples.mean(axis=1, dtype=np.float32)
    renditions: dict[int, bytes] = {}
    warnings: list[dict[str, str]] = []
    for rate in sorted(set(rates)):
        # soxr.resample is one-shot, so no stream state leaks between rate pairs.
        audio = mono if rate == source_rate else soxr.resample(mono, source_rate, rate)
        if not renditions:
            # The lowest rate is the phone's, and the one that loses the most.
            warnings = _phone_band_warnings(audio, rate)
        out = io.BytesIO()
        sf.write(out, audio, rate, format="MP3")
        renditions[rate] = out.getvalue()
    return IngestedSound(
        duration_ms=round(samples.shape[0] * 1000 / source_rate),
        renditions=renditions,
        warnings=warnings,
    )
