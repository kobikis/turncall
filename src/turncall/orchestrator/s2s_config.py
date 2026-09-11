"""S2S (speech-to-speech) service factory.

Maps S2SConfig to Pipecat's OpenAIRealtimeLLMService, OpenAILiveLLMService,
GeminiLiveLLMService or AWSNovaSonicLLMService with appropriate session
properties, voice, and turn detection settings.
"""

from typing import Any

from loguru import logger

from turncall.domain.models import AgentConfig

# S2SConfig's defaults are OpenAI Realtime's. Nova Sonic needs its own, applied
# only when the agent left the field untouched — an explicitly wrong model is
# surfaced by AWS rather than silently swapped. See ADR-0016.
_OPENAI_DEFAULT_MODEL = "gpt-realtime-2.1"
_OPENAI_DEFAULT_VOICE = "alloy"
_NOVA_SONIC_MODEL = "amazon.nova-2-sonic-v1:0"
_NOVA_SONIC_VOICE = "matthew"
_OPENAI_LIVE_MODEL = "gpt-live-1"


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
    if provider == "openai_live":
        return _create_openai_live(config, openai_api_key)
    if provider == "aws":
        return _create_nova_sonic(config)
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


def _create_nova_sonic(config: AgentConfig) -> Any:
    """Create a Nova Sonic 2 S2S service from S2SConfig + the agent's aws block."""
    from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService

    # Imported as a module, not a name: create_client() below re-resolves on
    # every session rollover, and binding late keeps that observable.
    from turncall.services import aws_credentials

    class _RefreshingNovaSonic(AWSNovaSonicLLMService):  # type: ignore[misc]
        """Re-resolves AWS credentials every time a Bedrock client is built.

        Nova Sonic sessions expire at ~6 minutes and Pipecat rolls over to a new
        one, which calls create_client() again through the
        NovaSonicSessionSender protocol. Temporary credentials (assume-role,
        IRSA) are typically good for about an hour, so without refreshing here a
        long call would die mid-conversation when they expired — and the caller
        would just hear the line go quiet. See ADR-0016.
        """

        def __init__(self, *, aws_config: Any, **kwargs: Any) -> None:
            self._turncall_aws = aws_config
            super().__init__(**kwargs)

        def create_client(self) -> Any:
            fresh = aws_credentials.resolve_aws_credentials(self._turncall_aws)
            self._access_key_id = fresh.access_key_id
            self._secret_access_key = fresh.secret_access_key
            self._session_token = fresh.session_token
            self._region = fresh.region
            return super().create_client()

    s2s = config.s2s
    credentials = aws_credentials.resolve_aws_credentials(config.aws)

    model = _NOVA_SONIC_MODEL if s2s.model == _OPENAI_DEFAULT_MODEL else s2s.model
    voice = _NOVA_SONIC_VOICE if s2s.voice == _OPENAI_DEFAULT_VOICE else s2s.voice

    settings_kwargs: dict[str, Any] = {"model": model, "voice": voice}
    system_prompt = _build_s2s_system_prompt(config)
    if system_prompt:
        settings_kwargs["system_instruction"] = system_prompt
    if s2s.temperature is not None:
        settings_kwargs["temperature"] = s2s.temperature
    if s2s.max_tokens is not None:
        settings_kwargs["max_tokens"] = s2s.max_tokens
    # Nova Sonic endpoints server-side; its sensitivity knob has no home in
    # turn_detection's server_vad|pipecat_vad enum, so it rides in extra.
    sensitivity = s2s.extra.get("endpointing_sensitivity")
    if sensitivity:
        settings_kwargs["endpointing_sensitivity"] = sensitivity

    service = _RefreshingNovaSonic(
        aws_config=config.aws,
        settings=AWSNovaSonicLLMService.Settings(**settings_kwargs),
        **credentials.nova_sonic_kwargs(),
    )

    logger.info(
        "S2S service created: provider=aws model={model} voice={voice} "
        "region={region} temporary_credentials={temp}",
        model=model,
        voice=voice,
        region=credentials.region,
        temp=credentials.session_token is not None,
    )
    return service


def _create_openai_live(config: AgentConfig, api_key: str) -> Any:
    """Create an OpenAILiveLLMService (gpt-live-1) from S2SConfig.

    Unlike Realtime, the live model is full duplex: it listens and speaks at
    once and decides for itself when to answer and when to stop on being
    talked over. There is no client-side turn detection to configure — the
    schema rejects `pipecat_vad` for this provider — and the pipeline just
    streams audio in and out.

    It can also *delegate* reasoning and tool calls to a backend text model
    while the conversation keeps going. Set `s2s.extra.backend_model` to turn
    that on; OpenAI hosts the backend, and the function calls it makes are run
    by our own tool handlers. Left unset, the live model answers on its own.
    """
    from pipecat.services.openai.live.llm import OpenAILiveLLMService
    from pipecat.services.openai.responses.llm import OpenAIResponsesLLMSettings

    s2s = config.s2s

    # S2SConfig's default model is OpenAI Realtime's. Swap only when the agent
    # left it untouched, so an explicitly wrong model is reported by OpenAI
    # rather than silently replaced. Same rule as Nova Sonic (ADR-0016).
    model = _OPENAI_LIVE_MODEL if s2s.model == _OPENAI_DEFAULT_MODEL else s2s.model

    settings_kwargs: dict[str, Any] = {"model": model, "voice": s2s.voice}
    system_prompt = _build_s2s_system_prompt(config)
    if system_prompt:
        settings_kwargs["system_instruction"] = system_prompt
    if s2s.temperature is not None:
        settings_kwargs["temperature"] = s2s.temperature
    if s2s.max_tokens is not None:
        settings_kwargs["max_tokens"] = s2s.max_tokens

    service_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "settings": OpenAILiveLLMService.Settings(**settings_kwargs),
    }
    if s2s.base_url:
        service_kwargs["base_url"] = s2s.base_url

    backend_model = s2s.extra.get("backend_model")
    if backend_model:
        service_kwargs["delegation"] = OpenAILiveLLMService.ResponsesDelegation(
            settings=OpenAIResponsesLLMSettings(model=backend_model),
            service_tier=s2s.extra.get("service_tier"),
        )

    service = OpenAILiveLLMService(**service_kwargs)

    logger.info(
        "S2S service created: provider=openai_live model={model} voice={voice} "
        "backend={backend} base_url={base_url}",
        model=model,
        voice=s2s.voice,
        backend=backend_model or "none (live model answers directly)",
        base_url=s2s.base_url or "openai-default",
    )
    return service
