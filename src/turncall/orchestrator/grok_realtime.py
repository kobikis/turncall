"""Grok Voice's pipecat service as TurnCall builds it (#386, #389).

Imports pipecat at module level, so `s2s_config` imports it only when it
builds an `xai` agent's service; `grok_voice` is the half the API schema reads.
"""

from typing import cast

from pipecat.services.xai.realtime import events
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

from turncall.orchestrator.xai_errors import XAIKeyRefusal


class KeyAwareGrokRealtime(XAIKeyRefusal, GrokRealtimeLLMService):
    """Grok Voice, reading xAI's refusal of a key as one (`XAIKeyRefusal`)."""


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
