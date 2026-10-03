"""S2S (speech-to-speech) service factory.

Maps S2SConfig to Pipecat's OpenAIRealtimeLLMService, OpenAILiveLLMService,
GeminiLiveLLMService or AWSNovaSonicLLMService with appropriate session
properties, voice, and turn detection settings.
"""

from collections.abc import Sequence
from typing import Any

from loguru import logger

from turncall.domain.models import (
    AgentConfig,
    AWSConfig,
    DelegationConfig,
    ToolDefinition,
    resolve_delegation,
)
from turncall.services.credentials import Credentials

# S2SConfig's defaults are OpenAI Realtime's. Nova Sonic needs its own, applied
# only when the agent left the field untouched — an explicitly wrong model is
# surfaced by AWS rather than silently swapped. See ADR-0016.
_OPENAI_DEFAULT_MODEL = "gpt-realtime-2.1"
_OPENAI_DEFAULT_VOICE = "alloy"
_NOVA_SONIC_MODEL = "amazon.nova-2-sonic-v1:0"
_NOVA_SONIC_VOICE = "matthew"
_OPENAI_LIVE_MODEL = "gpt-live-1"
_GEMINI_LIVE_MODEL = "gemini-3.8-live"
_GEMINI_LIVE_VOICE = "Charon"


def _say_first(first_message: str) -> str:
    return f'Start the conversation by saying exactly: "{first_message}"'


def _build_s2s_system_prompt(config: AgentConfig) -> str:
    """Build system prompt with first_message instruction for S2S."""
    prompt = config.system_prompt or ""
    if config.first_message:
        prompt += f"\n\nIMPORTANT: {_say_first(config.first_message)}"
    return prompt


def s2s_opening_messages(config: AgentConfig) -> list[dict[str, str]]:
    """The messages an S2S session's context starts with (#248).

    gpt-live-1 is full duplex and does not act on the instructions' "start the
    conversation" until it hears speech, so a caller waiting for a greeting
    heard silence. A trailing developer message is what pipecat delivers as
    commentary once the session starts — the API's way of asking the model to
    speak first. Realtime and Gemini Live greet from the empty run, and are
    left alone.
    """
    if config.s2s.provider != "openai_live":
        return []
    text = (
        _say_first(config.first_message)
        if config.first_message
        else "Greet the person on the call."  # inbound or outbound alike
    )
    return [{"role": "developer", "content": text}]


def create_s2s_service(
    config: AgentConfig,
    credentials: Credentials,
    tools: Sequence[ToolDefinition] = (),
    delegate: Any = None,
    hangup_gate: Any = None,
) -> Any:
    """Create the appropriate S2S service based on provider.

    `tools` is what the call advertises (static + MCP, deduplicated). Only
    `openai_live` reads it here, to tell gpt-live-1 what it can delegate; every
    other provider gets the tools themselves through the pipeline's context.
    `delegate` is the worker running the agent's own llm, for client mode, and
    `hangup_gate` the call's gate, armed as a delegation's goodbye is relayed
    (#262).
    """
    provider = config.s2s.provider
    if provider == "openai":
        return _create_openai_realtime(config, credentials.api_key("openai"))
    if provider == "google":
        return _create_gemini_live(config, credentials.api_key("google"))
    if provider == "openai_live":
        return _create_openai_live(
            config, credentials.api_key("openai"), tools, delegate, hangup_gate
        )
    if provider == "aws":
        return _create_nova_sonic(config, credentials.aws(config.aws))
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

    # S2SConfig defaults to OpenAI's model and voice whatever the provider, so
    # a `provider: google` agent that names neither arrives here holding
    # `gpt-realtime-2.1` and `alloy`. Swap the sentinels for Gemini's own, the
    # way the aws and openai_live paths already do — an explicitly chosen value
    # is passed through untouched, so a wrong one is reported by Google rather
    # than silently replaced (ADR-0016's rule).
    #
    # The model matters most: Gemini closes the socket with 1008
    # ("models/gpt-realtime-2.1 is not found ... for bidiGenerateContent"), so
    # the call died at connect. The voice is tolerated — Gemini ignores an
    # unknown name and uses its default — but sending `alloy` claims a voice
    # the caller will not hear.
    model = _GEMINI_LIVE_MODEL if s2s.model == _OPENAI_DEFAULT_MODEL else s2s.model
    voice = _GEMINI_LIVE_VOICE if s2s.voice == _OPENAI_DEFAULT_VOICE else s2s.voice

    settings_kwargs: dict[str, Any] = {
        "model": model,
        "voice": voice,
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
        model=model,
        voice=voice,
        turn=s2s.turn_detection,
    )
    return service


def _create_nova_sonic(config: AgentConfig, aws: AWSConfig) -> Any:
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
    credentials = aws_credentials.resolve_aws_credentials(aws)

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
        aws_config=aws,
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


# Client mode with `end_call` (#262). gpt-live-1 answered the caller's "bye"
# itself, then voiced the delegate's goodbye too: the delegate's transcript
# ends where its delegation began, so it cannot know one was said. Measured on
# the eval loopback: one goodbye in 8 of 8 calls with this line, two in about
# half without it (and in 4 of 5 real calls). It may still fill the wait ("Sure
# thing"); the gate is armed so that is not taken for the goodbye.
GOODBYE_TO_THE_DELEGATE = (
    "When the caller says goodbye, delegate ending the call and stay silent "
    "until the result comes back: the result is your goodbye, so do not say "
    "one before it."
)


def _delegation_paragraph(
    tools: Sequence[ToolDefinition], *, client: bool = False
) -> str:
    """Tell gpt-live-1 when to delegate, from the agent's own tool list.

    The live model decides by itself whether to delegate, and is known to
    acknowledge a request and then never hand it over. Naming what the
    delegate can do is what lets an existing prompt work without a rewrite.
    """
    lines = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    goodbye = (
        f"\n\n{GOODBYE_TO_THE_DELEGATE}"
        if client and any(tool.name == "end_call" for tool in tools)
        else ""
    )
    return (
        "\n\nYou cannot take actions yourself, but you can delegate. Delegate "
        "whenever one of the actions below fits the moment: the caller wants "
        "something done, asks for something you would need to look up, or the "
        "conversation reaches the point an action is for — ending the call "
        "when the caller says goodbye, for one. Never just say you will do it, "
        "and never answer from memory instead; delegate, then tell the caller "
        "what came back. Delegating is how these get done:\n" + lines + goodbye
    )


def _responses_delegation(config: AgentConfig, delegation: DelegationConfig) -> Any:
    from pipecat.services.openai.live.llm import ResponsesDelegation
    from pipecat.services.openai.responses.llm import (
        OpenAIResponsesLLMSettings,
        OpenAIResponsesReasoningConfig,
    )

    settings_kwargs: dict[str, Any] = {"model": delegation.model}
    # The agent's prompt reaches the delegate too, unless the block replaces it.
    instructions = delegation.instructions or config.system_prompt
    if instructions:
        settings_kwargs["system_instruction"] = instructions
    if delegation.reasoning_effort:
        settings_kwargs["reasoning"] = OpenAIResponsesReasoningConfig(
            effort=delegation.reasoning_effort
        )
    return ResponsesDelegation(
        settings=OpenAIResponsesLLMSettings(**settings_kwargs),
        service_tier=delegation.service_tier,
    )


def _create_openai_live(
    config: AgentConfig,
    api_key: str,
    tools: Sequence[ToolDefinition] = (),
    delegate: Any = None,
    hangup_gate: Any = None,
) -> Any:
    """Create an OpenAILiveLLMService (gpt-live-1) from S2SConfig.

    Unlike Realtime, the live model is full duplex: it listens and speaks at
    once and decides for itself when to answer and when to stop on being
    talked over. There is no client-side turn detection to configure — the
    schema rejects `pipecat_vad` for this provider — and the pipeline just
    streams audio in and out.

    It calls no tools itself: it *delegates* tool and reasoning work to a
    delegate while the conversation keeps going (CONTEXT.md, "Delegation").
    In client mode (#244) the delegate is the agent's own llm, running in
    pipecat's backend worker (`delegate`) with TurnCall's handlers on it. In
    responses mode OpenAI hosts the delegate, and this service runs the
    function calls it makes with TurnCall's handlers, in the call's own
    pipeline — so `end_call`'s hangup gate works as on every provider.
    Without a delegation pipecat declines every delegation, and the live
    model answers on its own.
    """
    from pipecat.services.openai.live.llm import (
        ClientDelegation,
        OpenAILiveLLMService,
    )

    from turncall.orchestrator.delegation_events import DelegationTrackingLiveService

    s2s = config.s2s
    delegation = resolve_delegation(s2s, has_tools=bool(tools))

    # S2SConfig's default model is OpenAI Realtime's. Swap only when the agent
    # left it untouched, so an explicitly wrong model is reported by OpenAI
    # rather than silently replaced. Same rule as Nova Sonic (ADR-0016).
    model = _OPENAI_LIVE_MODEL if s2s.model == _OPENAI_DEFAULT_MODEL else s2s.model

    settings_kwargs: dict[str, Any] = {"model": model, "voice": s2s.voice}
    system_prompt = _build_s2s_system_prompt(config)
    if delegation is not None and tools:
        system_prompt += _delegation_paragraph(
            tools, client=delegation.mode == "client"
        )
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

    if delegation is not None and delegation.mode == "client":
        if delegate is None:
            raise ValueError("client delegation needs its delegate worker")
        service_kwargs["delegation"] = ClientDelegation(
            backend=delegate, timeout_secs=delegation.timeout_seconds
        )
        service_kwargs["hangup_gate"] = hangup_gate
    elif delegation is not None:
        service_kwargs["delegation"] = _responses_delegation(config, delegation)
        # pipecat's ResponsesDelegation carries no timeout of its own — the
        # Responses run is OpenAI's. What runs here is each function call the
        # delegate makes, so that is what the timeout bounds; on expiry the
        # call is cancelled and the live model is told it did not complete.
        service_kwargs["function_call_timeout_secs"] = delegation.timeout_seconds

    service = DelegationTrackingLiveService(**service_kwargs)

    logger.info(
        "S2S service created: provider=openai_live model={model} voice={voice} "
        "delegation={delegation} base_url={base_url}",
        model=model,
        voice=s2s.voice,
        delegation=(
            f"{delegation.mode}:{delegation.model or config.llm.provider}"
            if delegation
            else "none (live model answers directly)"
        ),
        base_url=s2s.base_url or "openai-default",
    )
    return service
