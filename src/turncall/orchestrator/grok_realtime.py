"""Grok Voice's pipecat service as TurnCall builds it (#386, #389).

Imports pipecat at module level, so `s2s_config` imports it only when it
builds an `xai` agent's service; `grok_voice` is the half the API schema reads.
"""

from typing import cast

from pipecat.services.xai.realtime import events
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService
from pipecat.utils.errors import ErrorCategory, extract_http_status_code


class KeyAwareGrokRealtime(GrokRealtimeLLMService):
    """Reads xAI's refusal of a key as one.

    xAI answers a wrong key's handshake with HTTP 400 and "Incorrect API key
    provided" (measured), which pipecat files as a bad request, so the call's
    `error.raised` would not say which key to fix. A 401 or 403 is already
    classified by pipecat.
    """

    def _classify_error(self, exception: Exception) -> ErrorCategory | None:
        body = getattr(getattr(exception, "response", None), "body", None)
        if (
            extract_http_status_code(exception) == 400
            and isinstance(body, bytes)
            and b"API key" in body
        ):
            return ErrorCategory.AUTHENTICATION
        return super()._classify_error(exception)


def pcm_audio(sample_rate: int) -> events.AudioConfiguration:
    """PCM both ways at `sample_rate`: what the pipeline sends Grok the
    caller's audio at, and resamples Grok's from.

    Left unset, pipecat fills each direction from the pipeline's own rates in
    `setup()` — 16 kHz in by default — while the S2S pipeline resamples the
    caller to its service rate, so Grok would hear 24 kHz audio declared as
    16 kHz.
    """
    rate = cast(events.SUPPORTED_SAMPLE_RATES, sample_rate)
    return events.AudioConfiguration(
        input=events.AudioInput(format=events.PCMAudioFormat(rate=rate)),
        output=events.AudioOutput(format=events.PCMAudioFormat(rate=rate)),
    )


def turn_detection(mode: str) -> events.TurnDetection | None:
    """`pipecat_vad` is pipecat's manual mode: no server VAD, so the
    pipeline's VAD ends the turn and the service commits the buffer itself."""
    return None if mode == "pipecat_vad" else events.TurnDetection(type="server_vad")
