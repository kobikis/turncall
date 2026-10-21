"""What an `xai` LLM or TTS config cannot run as (#393, #395).

One rule, applied twice: at the API schema, and at build, since a call-init
inline agent is validated by the domain model rather than that schema (the
`grok_voice.refusal` and `deepgram_flux.mixes_flux` precedent). Imports no
pipecat, so API validation loads none (#389).
"""

from typing import Protocol

# xAI answers 400 to a speed outside this (measured, #395), on HTTP and at the
# WebSocket handshake, which would cost every reply its audio.
XAI_TTS_SPEED_RANGE = (0.7, 1.5)


class LLMSettings(Protocol):
    """What `llm_refusal` reads: the API's LLM schema and the domain's LLMConfig."""

    @property
    def base_url(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> str | None: ...


class TTSSettings(Protocol):
    """What `tts_refusal` reads: the API's TTS schema and the domain's TTSConfig."""

    @property
    def speed(self) -> float: ...


def llm_refusal(llm: LLMSettings) -> str | None:
    """Why an `xai` LLM config cannot run as written, or None."""
    if llm.base_url:
        # The route calls xAI's own endpoint; one elsewhere would be sent the
        # project's xAI key.
        return "base_url is not supported for the xai provider"
    if llm.reasoning_effort == "minimal":
        # OpenAI's lowest effort; xAI's are none|low|medium|high|xhigh.
        return "reasoning_effort 'minimal' is not an xAI value (low|medium|high)"
    return None


def tts_refusal(tts: TTSSettings) -> str | None:
    """Why an `xai` TTS config cannot run as written, or None."""
    low, high = XAI_TTS_SPEED_RANGE
    if not low <= tts.speed <= high:
        return f"tts.speed on xai must be between {low} and {high}"
    return None
