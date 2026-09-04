"""Config-driven Pipecat pipeline construction.

Reads an AgentConfig from the database and builds a fully-wired
Pipecat pipeline with the appropriate STT, LLM, TTS services,
context aggregation, and observability.
"""

import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.openai.llm import OpenAILLMService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.config.settings import BYOMSettings, PipecatSettings
from turncall.domain.models import AgentConfig
from turncall.orchestrator.observability import ObservabilityProcessor


@dataclass(frozen=True)
class CallContext:
    """Per-call context passed through the pipeline."""

    call_id: UUID
    project_id: UUID
    agent_id: UUID
    call_sid: str
    stream_sid: str
    session_factory: async_sessionmaker[AsyncSession]
    mcp_manager: Any | None = None  # MCPSessionManager (optional)


def _create_stt_service(
    config: AgentConfig, openai_api_key: str, *, sample_rate: int = 8000
) -> Any:
    """Create STT service. Supports deepgram, openai, elevenlabs, and cartesia."""
    provider = config.stt.provider

    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            sample_rate=sample_rate,
            encoding="linear16",
            settings=DeepgramSTTService.Settings(
                model=config.stt.model or "nova-3-general",
                language=config.stt.language or "en",
                interim_results=True,
                punctuate=True,
                smart_format=True,
            ),
        )
        stt._sample_rate = sample_rate
        return stt

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.stt import ElevenLabsSTTService

        from turncall.adapters.aiohttp_client import get_aiohttp_session

        stt = ElevenLabsSTTService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            aiohttp_session=get_aiohttp_session(),
            sample_rate=sample_rate,
            settings=ElevenLabsSTTService.Settings(
                model=config.stt.model or "scribe_v1",
                language=config.stt.language or "en",
            ),
        )
        stt._sample_rate = sample_rate
        return stt

    if provider == "openai":
        from pipecat.services.openai.stt import OpenAISTTService

        return OpenAISTTService(
            api_key=openai_api_key,
            settings=OpenAISTTService.Settings(model=config.stt.model),
        )

    if provider == "cartesia":
        from pipecat.services.cartesia.stt import CartesiaSTTService

        api_key = os.environ.get("CARTESIA_API_KEY", "")
        if not api_key:
            raise ValueError(
                "CARTESIA_API_KEY environment variable is required for Cartesia STT"
            )
        stt = CartesiaSTTService(
            api_key=api_key,
            sample_rate=sample_rate,
            settings=CartesiaSTTService.Settings(
                model=config.stt.model or "ink-whisper",
                language=config.stt.language or "en",
            ),
        )
        stt._sample_rate = sample_rate
        return stt

    raise ValueError(f"Unsupported STT provider: {provider}")


def _validate_byom_url(base_url: str, byom_settings: BYOMSettings) -> None:
    """Validate base_url against BYOM allowlist settings."""
    if not byom_settings.enabled:
        raise ValueError("BYOM (custom LLM providers) is disabled")
    if not byom_settings.allowed_url_patterns:
        return  # Empty allowlist = all URLs allowed (dev mode)
    import fnmatch

    for pattern in byom_settings.allowed_url_patterns:
        if fnmatch.fnmatch(base_url, pattern):
            return
    raise ValueError(
        f"base_url '{base_url}' not in allowed patterns: {byom_settings.allowed_url_patterns}"
    )


# Sentinel for _create_llm_service's reasoning_effort override: distinguishes
# "caller didn't say" (use the agent's config) from an explicit None (force off,
# e.g. the deterministic voicemail classifier).
_USE_CONFIG: Any = object()


def _openai_extra_body(
    reasoning_effort: str | None, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the OpenAI `extra` dict, folding reasoning_effort into extra_body
    alongside any base body (e.g. OpenRouter's `models`). Empty when nothing to
    send, so the request is byte-identical to before when unused."""
    body = dict(base or {})
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    return {"extra_body": body} if body else {}


def _create_llm_service(
    config: AgentConfig,
    openai_api_key: str,
    *,
    anthropic_api_key: str = "",
    openrouter_api_key: str = "",
    byom_settings: BYOMSettings | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = _USE_CONFIG,
) -> Any:
    """Create LLM service. Supports openai, ollama, custom_openai, anthropic, openrouter.

    temperature/max_tokens default to the agent's llm config; pass explicit
    values to override (e.g. the voicemail classifier pins a low temperature).
    reasoning_effort defaults to the agent's config; pass explicit None to force
    it off (e.g. the deterministic voicemail classifier). OpenAI-family only.
    """
    provider = config.llm.provider
    temperature = temperature if temperature is not None else config.llm.temperature
    max_tokens = max_tokens if max_tokens is not None else config.llm.max_tokens
    effort = (
        config.llm.reasoning_effort
        if reasoning_effort is _USE_CONFIG
        else reasoning_effort
    )

    if provider == "openrouter":
        # OpenRouter is OpenAI-compatible; fallback_models ride in extra_body as
        # the `models` array (primary first), tried in order on rate-limit/error.
        # See ADR-0003. reasoning_effort folds into the same extra_body.
        models = (
            {"models": [config.llm.model, *config.llm.fallback_models]}
            if config.llm.fallback_models
            else None
        )
        return OpenAILLMService(
            api_key=config.llm.api_key or openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            settings=OpenAILLMService.Settings(
                model=config.llm.model,
                extra=_openai_extra_body(effort, models),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "openai":
        return OpenAILLMService(
            api_key=openai_api_key,
            settings=OpenAILLMService.Settings(
                model=config.llm.model,
                extra=_openai_extra_body(effort),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        resolved_key = config.llm.api_key or anthropic_api_key
        return AnthropicLLMService(
            api_key=resolved_key,
            settings=AnthropicLLMService.Settings(
                model=config.llm.model,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "ollama":
        from pipecat.services.ollama.llm import OLLamaLLMService

        base_url = config.llm.base_url or "http://localhost:11434/v1"
        if byom_settings:
            _validate_byom_url(base_url, byom_settings)
        return OLLamaLLMService(
            base_url=base_url,
            settings=OLLamaLLMService.Settings(
                model=config.llm.model,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "custom_openai":
        base_url = config.llm.base_url
        if not base_url:
            raise ValueError("base_url is required for custom_openai provider")
        if byom_settings:
            _validate_byom_url(base_url, byom_settings)
        return OpenAILLMService(
            api_key=config.llm.api_key or "no-key",
            base_url=base_url,
            settings=OpenAILLMService.Settings(
                model=config.llm.model,
                extra=_openai_extra_body(effort),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


def _create_tts_service(config: AgentConfig, openai_api_key: str) -> Any:
    """Create TTS service. Supports deepgram, openai, elevenlabs, and cartesia."""
    provider = config.tts.provider

    # LLMs emit markdown (**bold**, `code`) that TTS would otherwise read aloud
    # literally ("asterisk asterisk"). Strip it on the way into every voice.
    # Pipecat wants (aggregation_type | "*", transform) tuples; "*" = all types.
    from pipecat.utils.text.transforms import strip_markdown

    text_transforms = [("*", strip_markdown)]

    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        voice = config.tts.voice or "aura-2-helena-en"
        return DeepgramTTSService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            settings=DeepgramTTSService.Settings(voice=voice),
            text_transforms=text_transforms,
        )

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        # ElevenLabs uses voice IDs (e.g. "21m00Tcm4TlvDq8ikWAM" for Rachel)
        # or voice names via Settings. Pass as voice in Settings.
        return ElevenLabsTTSService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(
                voice=config.tts.voice or "Rachel",
                model=config.tts.model or "eleven_flash_v2_5",
            ),
            text_transforms=text_transforms,
        )

    if provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(
            api_key=openai_api_key,
            settings=OpenAITTSService.Settings(
                model=config.tts.model,
                voice=config.tts.voice,
            ),
            text_transforms=text_transforms,
        )

    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        api_key = os.environ.get("CARTESIA_API_KEY", "")
        if not api_key:
            raise ValueError(
                "CARTESIA_API_KEY environment variable is required for Cartesia TTS"
            )
        tts_settings = CartesiaTTSService.Settings(
            model=config.tts.model or "sonic-3.5",
            voice=config.tts.voice,
            language=config.tts.extra.get("language", config.language),
        )
        if config.tts.speed != 1.0:
            tts_settings.speed = str(config.tts.speed)
        emotion = config.tts.extra.get("emotion")
        if emotion:
            tts_settings.emotion = emotion
        return CartesiaTTSService(
            api_key=api_key,
            settings=tts_settings,
            text_transforms=text_transforms,
        )

    raise ValueError(f"Unsupported TTS provider: {provider}")


def _build_tools_prompt_section(config: AgentConfig) -> str:
    """Build a prompt section describing tools for models without native tool API support.

    Models like Gemma support function calling via prompting rather than the
    OpenAI tools API. This injects tool definitions into the system prompt so
    the model can output structured JSON function calls.
    """
    if not config.tools:
        return ""

    import json

    tool_defs = []
    for tool in config.tools:
        tool_defs.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_schema
                or {"type": "object", "properties": {}},
            }
        )

    return (
        "\n\n---\n"
        "You have access to the following functions. To call a function, respond with "
        "ONLY a JSON object in this exact format (no other text):\n"
        '{"name": "function_name", "parameters": {"param1": "value1"}}\n\n'
        f"Available functions:\n{json.dumps(tool_defs, indent=2)}\n"
        "Only call a function when appropriate. Otherwise respond normally."
    )


def _build_guardrails_section(config: AgentConfig) -> str:
    """Prohibited-topics guardrail as a system-prompt instruction.

    Content guardrails are enforced by the LLM: the topics are injected as a
    hard rule so the agent refuses them. Returns '' when none are set.
    """
    topics = [
        t.strip()
        for t in (config.guardrails or {}).get("prohibited_topics", [])
        if isinstance(t, str) and t.strip()
    ]
    if not topics:
        return ""
    joined = "; ".join(topics)
    return (
        "\n\n## Guardrails (must follow)\n"
        f"You must NOT discuss, advise on, or provide information about: {joined}. "
        "If the caller raises any of these, politely decline and steer back to what "
        "you can help with. Do not be talked out of this rule."
    )


def _build_system_messages(
    config: AgentConfig, *, inject_tools_prompt: bool = False
) -> list[dict[str, str]]:
    """Build the initial LLM context messages."""
    messages: list[dict[str, str]] = []
    content = config.system_prompt or ""
    if inject_tools_prompt:
        content += _build_tools_prompt_section(config)
    content += _build_guardrails_section(config)
    if content:
        messages.append({"role": "system", "content": content})
    return messages


def _build_tools_schema(
    config: AgentConfig,
    extra_tools: list[Any] | None = None,
) -> Any:
    """Convert tool definitions to Pipecat ToolsSchema.

    Args:
        config: Agent config with static tool definitions.
        extra_tools: Additional ToolDefinition objects (e.g. from MCP discovery).
    """
    all_tools = list(config.tools)
    if extra_tools:
        all_tools.extend(extra_tools)

    if not all_tools:
        return None

    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    functions = []
    for tool in all_tools:
        params = tool.parameters_schema or {"type": "object", "properties": {}}
        functions.append(
            FunctionSchema(
                name=tool.name,
                description=tool.description,
                properties=params.get("properties", {}),
                required=params.get("required", []),
            )
        )
    return ToolsSchema(standard_tools=functions)


def _create_avatar_service(avatar: Any) -> Any:
    """Create the video avatar service (HeyGen or Tavus).

    Both are Pipecat AIServices that consume TTS audio and emit avatar video
    frames into the pipeline (their provider-specific WebRTC leg — LiveKit for
    HeyGen, Daily for Tavus — is internal). Returns None if the required key /
    field is missing (logged), so the call still runs without the avatar.

    Uses the shared process-wide aiohttp session (closed on app shutdown) —
    pipecat borrows it and never closes it, so a per-call session would leak.
    """
    from turncall.adapters.aiohttp_client import get_aiohttp_session

    provider = avatar.provider
    if provider == "heygen":
        from pipecat.services.heygen.api_liveavatar import LiveAvatarNewSessionRequest
        from pipecat.services.heygen.client import ServiceType
        from pipecat.services.heygen.video import HeyGenVideoService

        key = os.environ.get("HEYGEN_LIVE_AVATAR_API_KEY", "")
        if not key:
            logger.warning("Avatar enabled but HEYGEN_LIVE_AVATAR_API_KEY unset; skipping")
            return None
        logger.info("Avatar enabled: HeyGen {aid}", aid=avatar.avatar_id)
        return HeyGenVideoService(
            api_key=key,
            service_type=ServiceType.LIVE_AVATAR,
            session=get_aiohttp_session(),
            session_request=LiveAvatarNewSessionRequest(
                is_sandbox=avatar.is_sandbox,
                avatar_id=avatar.avatar_id,
            ),
        )

    if provider == "tavus":
        from pipecat.services.tavus.video import TavusVideoService

        key = os.environ.get("TAVUS_API_KEY", "")
        if not key:
            logger.warning("Avatar enabled but TAVUS_API_KEY unset; skipping")
            return None
        if not avatar.replica_id:
            logger.warning("Tavus avatar requires replica_id; skipping")
            return None
        logger.info("Avatar enabled: Tavus {rid}", rid=avatar.replica_id)
        return TavusVideoService(
            api_key=key,
            replica_id=avatar.replica_id,
            persona_id=avatar.persona_id,
            session=get_aiohttp_session(),
        )

    logger.warning("Unknown avatar provider '{p}'; skipping", p=provider)
    return None


def create_pipeline(
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    openai_api_key: str,
    pipecat_settings: PipecatSettings,
    *,
    audio_sample_rate: int = 8000,
    byom_settings: BYOMSettings | None = None,
    google_api_key: str = "",
    anthropic_api_key: str = "",
    openrouter_api_key: str = "",
    knowledge_base_attachments: list[dict[str, Any]] | None = None,
    knowledge_preamble: str = "",
    mcp_tools: list[Any] | None = None,
    avatar_enabled: bool = False,
) -> Pipeline:
    """Build a Pipecat pipeline from an AgentConfig.

    avatar_enabled is set by the WebRTC caller (avatar is WebRTC + cascade only).
    """
    if config.pipeline_mode == "s2s":
        return _create_s2s_pipeline(
            config,
            transport,
            call_context,
            openai_api_key,
            pipecat_settings,
            audio_sample_rate=audio_sample_rate,
            google_api_key=google_api_key,
            byom_settings=byom_settings,
        )

    # --- Cascade pipeline (STT → LLM → TTS) ---
    # Create AI services
    stt = _create_stt_service(config, openai_api_key, sample_rate=audio_sample_rate)
    llm = _create_llm_service(
        config,
        openai_api_key,
        anthropic_api_key=anthropic_api_key,
        openrouter_api_key=openrouter_api_key,
        byom_settings=byom_settings,
    )
    tts = _create_tts_service(config, openai_api_key)

    # Build context with system prompt and tools
    # For BYOM providers (ollama, custom_openai), inject tools into the system
    # prompt instead of using the API-level tools parameter, since many local
    # models (e.g., Gemma) support function calling via prompting only.
    byom_provider = config.llm.provider in ("ollama", "custom_openai")
    if byom_provider:
        messages = _build_system_messages(config, inject_tools_prompt=True)
        if config.tools:
            logger.info(
                "BYOM mode: tools injected into system prompt for {provider}/{model}",
                provider=config.llm.provider,
                model=config.llm.model,
            )
        kwargs: dict[str, Any] = {"messages": messages}
    else:
        tools_schema = _build_tools_schema(config, extra_tools=mcp_tools)
        messages = _build_system_messages(config)
        kwargs = {"messages": messages}
        if tools_schema is not None:
            kwargs["tools"] = tools_schema

    # Prepend knowledge into the system prompt: prompt-mode full text (so it's
    # always present) + an awareness hint for auto/tool KBs. Built async upstream.
    if knowledge_preamble:
        if messages and messages[0].get("role") == "system":
            base = messages[0]["content"]
            messages[0] = {**messages[0], "content": f"{knowledge_preamble}\n\n{base}"}
        else:
            messages.insert(0, {"role": "system", "content": knowledge_preamble})
        kwargs["messages"] = messages

    context = LLMContext(**kwargs)

    # VAD + turn detection configuration (Pipecat 1.0: VAD lives on the user aggregator)
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregatorParams,
    )

    user_params_kwargs: dict[str, Any] = {
        "vad_analyzer": SileroVADAnalyzer(sample_rate=audio_sample_rate),
    }

    # Smart turn detection: ML model that understands conversational pauses
    if config.smart_turn_detection:
        try:
            from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
                LocalSmartTurnAnalyzerV3,
            )
            from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
                TurnAnalyzerUserTurnStopStrategy,
            )
            from pipecat.turns.user_turn_strategies import UserTurnStrategies

            turn_analyzer = LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=config.smart_turn_stop_secs),
            )
            stop_strategy = TurnAnalyzerUserTurnStopStrategy(
                turn_analyzer=turn_analyzer
            )
            user_params_kwargs["user_turn_strategies"] = UserTurnStrategies(
                stop=[stop_strategy]
            )
            logger.info("Smart turn detection enabled (LocalSmartTurnV3)")
        except Exception:
            logger.warning("Smart turn detection unavailable, using VAD-only")

    context_aggregator = LLMContextAggregatorPair(
        context=context,
        user_params=LLMUserAggregatorParams(**user_params_kwargs),
    )

    # Observability: transcript taps placed early to capture before aggregation
    from turncall.orchestrator.observability import (
        AssistantTranscriptTapProcessor,
        TranscriptTapProcessor,
    )

    customer_tap = TranscriptTapProcessor(call_context=call_context)
    assistant_tap = AssistantTranscriptTapProcessor(call_context=call_context, llm_service=llm)
    observability = ObservabilityProcessor(call_context=call_context)

    # Voicemail detection (for outbound calls)
    voicemail_detector = None
    if config.voicemail_detection.enabled:
        from pipecat.extensions.voicemail.voicemail_detector import VoicemailDetector

        vm_config = config.voicemail_detection
        backoff = vm_config.backoff_plan

        # Use a separate lightweight LLM for classification. Pin a deterministic
        # temperature and force reasoning off — a yes/no classifier must not
        # inherit the agent's conversational sampling (same pattern as call-analysis).
        classification_llm = _create_llm_service(
            config,
            openai_api_key,
            anthropic_api_key=anthropic_api_key,
            temperature=0.1,
            reasoning_effort=None,
        )
        voicemail_detector = VoicemailDetector(
            llm=classification_llm,
            voicemail_response_delay=backoff.start_at_seconds,
            custom_system_prompt=vm_config.custom_system_prompt,
        )

        # Track retry state
        _vm_retry_count = 0
        _vm_decided = False

        @voicemail_detector.event_handler("on_voicemail_detected")
        async def handle_voicemail(processor: Any) -> None:
            nonlocal _vm_retry_count, _vm_decided
            import asyncio

            if _vm_decided:
                return

            _vm_retry_count += 1

            # Retry: re-classify after frequency_seconds if under max_retries
            if _vm_retry_count < backoff.max_retries:
                logger.info(
                    "Voicemail tentative ({n}/{max}), retrying in {freq}s",
                    n=_vm_retry_count,
                    max=backoff.max_retries,
                    freq=backoff.frequency_seconds,
                )
                await asyncio.sleep(backoff.frequency_seconds)
                return  # Let next classification attempt run

            # Final decision: voicemail confirmed
            _vm_decided = True
            logger.info(
                "Voicemail confirmed: call={call_id} (after {n} checks)",
                call_id=str(call_context.call_id),
                n=_vm_retry_count,
            )

            # Wait for beep before leaving message
            if vm_config.beep_max_await_seconds > 0:
                logger.info(
                    "Waiting up to {s}s for beep",
                    s=vm_config.beep_max_await_seconds,
                )
                await asyncio.sleep(vm_config.beep_max_await_seconds)

            # Leave voicemail message
            from pipecat.frames.frames import TTSSpeakFrame

            if vm_config.voicemail_message:
                # 1.4 flipped append_to_context default to True; keep the
                # voicemail prompt out of the LLM context (call is ending).
                await processor.push_frame(
                    TTSSpeakFrame(
                        text=vm_config.voicemail_message,
                        append_to_context=False,
                    )
                )

            # Log event
            try:
                async with call_context.session_factory() as session:
                    from turncall.storage.repositories import call_repo

                    await call_repo.create_call_event(
                        session,
                        call_id=call_context.call_id,
                        event_type="voicemail.detected",
                        payload={"retries": _vm_retry_count},
                    )
                    await session.commit()
            except Exception:
                logger.exception("voicemail_event_error")

        @voicemail_detector.event_handler("on_conversation_detected")
        async def handle_conversation(processor: Any) -> None:
            nonlocal _vm_decided
            _vm_decided = True
            logger.info(
                "Human detected: call={call_id}",
                call_id=str(call_context.call_id),
            )

    # Knowledge base processor (auto mode)
    kb_processor = None
    if knowledge_base_attachments:
        auto_kb_ids = [
            att["knowledge_base_id"]
            for att in knowledge_base_attachments
            if att.get("mode") == "auto"
        ]
        if auto_kb_ids:
            from turncall.orchestrator.knowledge_processor import (
                KnowledgeRetrievalProcessor,
            )

            # Use settings from the first auto-mode attachment
            first_auto = next(
                a for a in knowledge_base_attachments if a.get("mode") == "auto"
            )
            kb_processor = KnowledgeRetrievalProcessor(
                knowledge_base_ids=auto_kb_ids,
                session_factory=call_context.session_factory,
                openai_api_key=openai_api_key,
                top_k=first_auto.get("top_k", 5),
                similarity_threshold=first_auto.get("similarity_threshold", 0.3),
            )
            logger.info("KB auto-retrieval enabled for {n} KBs", n=len(auto_kb_ids))

        # Register query_knowledge tool (tool mode)
        tool_kb_ids = [
            att["knowledge_base_id"]
            for att in knowledge_base_attachments
            if att.get("mode") == "tool"
        ]
        if tool_kb_ids:
            from turncall.orchestrator.knowledge_processor import (
                KNOWLEDGE_TOOL_SCHEMA,
                create_knowledge_tool_handler,
            )

            first_tool = next(
                a for a in knowledge_base_attachments if a.get("mode") == "tool"
            )
            handler = create_knowledge_tool_handler(
                knowledge_base_ids=tool_kb_ids,
                session_factory=call_context.session_factory,
                openai_api_key=openai_api_key,
                top_k=first_tool.get("top_k", 5),
                similarity_threshold=first_tool.get("similarity_threshold", 0.3),
            )
            llm.register_function("query_knowledge", handler)

            # Add tool schema to context if not BYOM
            if not byom_provider:
                from pipecat.adapters.schemas.function_schema import FunctionSchema
                from pipecat.adapters.schemas.tools_schema import ToolsSchema

                kb_func = FunctionSchema(
                    name=KNOWLEDGE_TOOL_SCHEMA["name"],
                    description=first_tool.get("tool_description")
                    or KNOWLEDGE_TOOL_SCHEMA["description"],
                    properties=KNOWLEDGE_TOOL_SCHEMA["parameters"]["properties"],
                    required=KNOWLEDGE_TOOL_SCHEMA["parameters"]["required"],
                )
                existing_tools = kwargs.get("tools")
                if existing_tools:
                    existing_tools.standard_tools.append(kb_func)
                else:
                    kwargs["tools"] = ToolsSchema(standard_tools=[kb_func])
                # Rebuild context with updated tools
                context = LLMContext(**kwargs)
                context_aggregator = LLMContextAggregatorPair(
                    context=context,
                    user_params=LLMUserAggregatorParams(**user_params_kwargs),
                )

            logger.info("KB tool mode enabled for {n} KBs", n=len(tool_kb_ids))

    # Video avatar: consumes TTS audio, emits avatar video. Sits right before
    # transport.output(). WebRTC + cascade only — gated by the avatar_enabled
    # flag from the caller.
    avatar = None
    if avatar_enabled and config.avatar.enabled:
        avatar = _create_avatar_service(config.avatar)

    # App-side recording: captures merged user+bot audio just BEFORE
    # transport.output() (which consumes audio frames and doesn't push them
    # downstream), then writes a WAV to object storage on call end. The flush is
    # driven by on_client_disconnected (Twilio hangup sends no end frame). All
    # transports.
    from turncall.orchestrator.call_recorder import attach_recorder

    recorder = attach_recorder(
        transport, call_context, sample_rate=audio_sample_rate
    )

    # Build pipeline (Pipecat 1.0: VAD is handled by the user aggregator)
    # customer_tap after STT: captures user speech before aggregator consumes it
    # assistant_tap after LLM: accumulates tokens, flushes on LLMFullResponseEndFrame
    if voicemail_detector:
        processors: list[Any] = [
            transport.input(),
            stt,
            customer_tap,
            voicemail_detector.detector(),
            context_aggregator.user(),
            *([kb_processor] if kb_processor else []),
            llm,
            assistant_tap,
            tts,
            voicemail_detector.gate(),
            *([avatar] if avatar else []),
            recorder,
            transport.output(),
            context_aggregator.assistant(),
            observability,
        ]
    else:
        processors = [
            transport.input(),
            stt,
            customer_tap,
            context_aggregator.user(),
            *([kb_processor] if kb_processor else []),
            llm,
            assistant_tap,
            tts,
            *([avatar] if avatar else []),
            recorder,
            transport.output(),
            context_aggregator.assistant(),
            observability,
        ]

    llm_info = f"{config.llm.provider}/{config.llm.model}"
    if config.llm.base_url:
        llm_info += f" @ {config.llm.base_url}"
    logger.info(
        "Pipeline created: STT={stt}/{stt_model} LLM={llm_info} TTS={tts}/{tts_voice}",
        stt=config.stt.provider,
        stt_model=config.stt.model,
        llm_info=llm_info,
        tts=config.tts.provider,
        tts_voice=config.tts.voice,
    )

    return Pipeline(processors)


def _create_s2s_pipeline(
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    openai_api_key: str,
    pipecat_settings: PipecatSettings,
    *,
    audio_sample_rate: int = 8000,
    google_api_key: str = "",
    byom_settings: BYOMSettings | None = None,
) -> Pipeline:
    """Build a speech-to-speech pipeline using OpenAI Realtime or Gemini Live.

    The S2S model handles STT + LLM + TTS in a single WebSocket connection.
    Pipeline is much simpler than cascade:
      transport.input → [VAD] → S2S_LLM → transport.output → context_agg → observability
    """
    from turncall.orchestrator.s2s_config import create_s2s_service

    # A gateway base_url is an attacker-influenceable outbound target — gate it
    # through the same BYOM allowlist as custom text-LLM endpoints.
    if config.s2s.base_url and byom_settings:
        _validate_byom_url(config.s2s.base_url, byom_settings)

    s2s_llm = create_s2s_service(config, openai_api_key, google_api_key=google_api_key)

    # Build context — system prompt + first_message are handled by the S2S
    # service directly (SessionProperties.instructions for OpenAI,
    # system_instruction for Gemini). Only pass tools via context.
    tools_schema = _build_tools_schema(config)
    context_kwargs: dict[str, Any] = {"messages": []}
    if tools_schema is not None:
        context_kwargs["tools"] = tools_schema
    context = LLMContext(**context_kwargs)

    # Pipecat 1.0: VAD lives on the user aggregator (needed for pipecat_vad turn detection)
    s2s_user_params = None
    if config.s2s.turn_detection == "pipecat_vad":
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMUserAggregatorParams,
        )

        s2s_user_params = LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(sample_rate=audio_sample_rate),
        )

    # realtime_service_mode: trailing context writes + auto-swapped turn
    # strategies for realtime S2S services (OpenAI Realtime / Gemini Live).
    # Orthogonal to vad_analyzer, so pipecat_vad mode keeps its Silero analyzer.
    context_aggregator = LLMContextAggregatorPair(
        context=context,
        user_params=s2s_user_params,
        realtime_service_mode=True,
    )

    # Transcript capture. The S2S service emits both the user TranscriptionFrame
    # (input_audio_transcription) and the assistant TextFrame — but in opposite
    # directions: user transcription goes UPSTREAM, assistant text DOWNSTREAM. So
    # customer_tap sits BEFORE s2s_llm (to see the upstream frame) and
    # assistant_tap AFTER it. (Both OpenAI Realtime and Gemini Live do this.)
    from turncall.orchestrator.observability import (
        AssistantTranscriptTapProcessor,
        TranscriptTapProcessor,
    )

    customer_tap = TranscriptTapProcessor(call_context=call_context)
    assistant_tap = AssistantTranscriptTapProcessor(
        call_context=call_context, llm_service=s2s_llm
    )

    # Observability
    observability = ObservabilityProcessor(call_context=call_context)

    # App-side recording (writes a WAV to object storage on call end). Flush is
    # driven by on_client_disconnected (hangup sends no end frame downstream).
    from turncall.orchestrator.call_recorder import attach_recorder

    recorder = attach_recorder(
        transport, call_context, sample_rate=audio_sample_rate
    )

    # Audio resamplers: Twilio sends 8kHz but S2S services expect 24kHz.
    # Two separate instances: input (8k→24k before LLM) and output (24k→8k after LLM).
    # Each processor can only appear once in a Pipecat pipeline.
    input_resampler = None
    output_resampler = None
    s2s_sample_rate = 24000
    if audio_sample_rate != s2s_sample_rate:
        from turncall.orchestrator.audio_resampler import AudioResampler

        input_resampler = AudioResampler(
            pipeline_sample_rate=audio_sample_rate,
            service_sample_rate=s2s_sample_rate,
        )
        output_resampler = AudioResampler(
            pipeline_sample_rate=audio_sample_rate,
            service_sample_rate=s2s_sample_rate,
        )
        logger.info(
            "S2S audio resampler: {src}Hz ↔ {dst}Hz",
            src=audio_sample_rate,
            dst=s2s_sample_rate,
        )

    # Build processor list based on turn detection mode
    # context_aggregator.user() is always needed — it sends the initial
    # LLMContextFrame that triggers the Realtime WebSocket connection.
    processors: list[Any] = [
        transport.input(),
        context_aggregator.user(),
        *([input_resampler] if input_resampler else []),
        customer_tap,
        s2s_llm,
        assistant_tap,
        *([output_resampler] if output_resampler else []),
        recorder,
        transport.output(),
        context_aggregator.assistant(),
        observability,
    ]

    logger.info(
        "S2S pipeline created: {provider}/{model} voice={voice} turn={turn}",
        provider=config.s2s.provider,
        model=config.s2s.model,
        voice=config.s2s.voice,
        turn=config.s2s.turn_detection,
    )

    return Pipeline(processors)
