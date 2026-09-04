"""S2S (speech-to-speech) service factory.

Maps S2SConfig to Pipecat's OpenAIRealtimeLLMService or GeminiLiveLLMService
with appropriate session properties, voice, and turn detection settings.
"""

from typing import Any

from loguru import logger

from turncall.domain.models import AgentConfig


def _build_s2s_system_prompt(config: AgentConfig) -> str:
    """Build system prompt with first_message instruction for S2S."""
    prompt = config.system_prompt or ""
    if config.first_message:
        prompt += (
            "\n\nIMPORTANT: Start the conversation by saying exactly: "
            f'"{config.first_message}"'
        )
    return prompt


def create_s2s_service(
    config: AgentConfig,
    openai_api_key: str,
    google_api_key: str = "",
) -> Any:
    """Create the appropriate S2S service based on provider."""
    provider = config.s2s.provider
    if provider == "openai":
        return _create_openai_realtime(config, openai_api_key)
    if provider == "google":
        return _create_gemini_live(config, google_api_key)
    raise ValueError(f"Unsupported S2S provider: {provider}")


def _create_openai_realtime(config: AgentConfig, api_key: str) -> Any:
    """Create an OpenAIRealtimeLLMService from S2SConfig."""
    from pipecat.services.openai.realtime.events import (
        AudioConfiguration,
        AudioInput,
        AudioOutput,
        InputAudioTranscription,
        SessionProperties,
        TurnDetection,
    )
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    s2s = config.s2s

    system_prompt = _build_s2s_system_prompt(config)

    # GA session shape: transcription/voice/turn-detection live under `audio`, and
    # modalities is `output_modalities`. The old top-level beta kwargs
    # (input_audio_transcription, voice, modalities) are silently DROPPED by the
    # current SessionProperties — which is why the user side of the transcript was
    # empty and the configured voice was ignored.
    turn_detection = TurnDetection() if s2s.turn_detection == "server_vad" else False
    session_properties = SessionProperties(
        # Audio only — ["text","audio"] made the model emit assistant text twice.
        output_modalities=["audio"],
        instructions=system_prompt or None,
        # None = provider default. Realtime GA has no temperature knob; the
        # schema rejects s2s.temperature for this provider.
        max_output_tokens=s2s.max_tokens,
        audio=AudioConfiguration(
            input=AudioInput(
                # No model= — inherit Pipecat's current realtime transcription
                # default (gpt-realtime-whisper) instead of pinning a stale one.
                # Constructing it at all is what keeps the user-side transcript
                # populated; only the model choice is delegated.
                transcription=InputAudioTranscription(),
                turn_detection=turn_detection,
            ),
            output=AudioOutput(voice=s2s.voice),
        ),
    )

    # A base_url points the realtime WebSocket at a gateway (Vercel AI Gateway,
    # LiteLLM) or xAI direct instead of api.openai.com — same protocol, so the
    # model string (e.g. "xai/grok-voice-think-fast-1.0") just routes upstream.
    service_kwargs: dict[str, Any] = {}
    if s2s.base_url:
        service_kwargs["base_url"] = s2s.base_url

    service = OpenAIRealtimeLLMService(
        api_key=api_key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=s2s.model,
            session_properties=session_properties,
        ),
        **service_kwargs,
    )

    logger.info(
        "S2S service created: provider=openai model={model} voice={voice} "
        "turn={turn} base_url={base_url}",
        model=s2s.model,
        voice=s2s.voice,
        turn=s2s.turn_detection,
        base_url=s2s.base_url or "openai-default",
    )
    return service


def _create_gemini_live(config: AgentConfig, api_key: str) -> Any:
    """Create a GeminiLiveLLMService from S2SConfig."""
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

    s2s = config.s2s

    settings_kwargs: dict[str, Any] = {
        "model": s2s.model,
        "voice": s2s.voice,
    }
    # Only pass sampling knobs when set — unset keeps Gemini's own defaults.
    if s2s.temperature is not None:
        settings_kwargs["temperature"] = s2s.temperature
    if s2s.max_tokens is not None:
        settings_kwargs["max_tokens"] = s2s.max_tokens

    # Turn detection: server_vad uses Gemini's built-in VAD,
    # pipecat_vad disables it so Pipecat's Silero VAD handles it
    if s2s.turn_detection == "pipecat_vad":
        from pipecat.services.google.gemini_live.llm import GeminiVADParams

        settings_kwargs["vad"] = GeminiVADParams(disabled=True)

    system_prompt = _build_s2s_system_prompt(config)

    service = GeminiLiveLLMService(
        api_key=api_key,
        system_instruction=system_prompt or None,
        settings=GeminiLiveLLMService.Settings(**settings_kwargs),
    )

    logger.info(
        "S2S service created: provider=google model={model} voice={voice} turn={turn}",
        model=s2s.model,
        voice=s2s.voice,
        turn=s2s.turn_detection,
    )
    return service
