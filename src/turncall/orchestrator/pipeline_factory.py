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
from turncall.services.llm_models import resolve_llm_model
from turncall.services.tool_mocks import ToolMocks

# A call whose agent came from call-init as an inline config (ADR-0008: the
# response carries `agent` rather than `agent_id`) has no agent row to point at.
# Every transport substitutes this sentinel so the pipeline can still build —
# anything that looks the agent up by it simply finds nothing, which is the
# truth. Without it a transport has to invent its own placeholder, and one of
# them invented a string and fed it to UUID().
DYNAMIC_AGENT_ID = UUID(int=0)


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
    # Set when this pipeline is an eval iteration rather than a call (ADR-0018).
    # There is no `calls` row behind it, so every call-scoped side effect --
    # status writes, call_events, transcript and call.* webhooks -- is off. The
    # eval's own record is the run row; the harness observes the conversation
    # over the wire.
    eval_run_id: UUID | None = None
    # The eval scenario's tool mocks + policy (#71). None on a real call, which
    # is what makes every tool dispatch below unconditional there.
    tool_mocks: ToolMocks | None = None

    @property
    def is_eval(self) -> bool:
        return self.eval_run_id is not None


def _overflow(extra: dict[str, Any], *managed: str) -> dict[str, Any]:
    """Keys from a customer's `extra` that we don't already set ourselves.

    Pipecat merges `extra` over the declared fields — `given_fields()` does
    `result.update(self.extra)` last — so a key naming a setting we manage
    would silently beat it. Only Deepgram STT has a promotion step that
    protects explicit fields; the other services don't, so filter instead of
    relying on it. Overflow means the keys we don't manage; the rest belong
    in their own config field.
    """
    return {k: v for k, v in extra.items() if k not in managed}


# Deepgram splits vocabulary hints across two mutually exclusive parameters and
# rejects the wrong one outright: `keyterm` is Nova-3 and Flux only ("`keyterm`
# is only supported for Nova-3 and Flux"), `keywords` is everything else
# ("Keywords are not supported for Nova-3"). Both are a 400 at connect, so the
# model decides the spelling, not the caller.
_DEEPGRAM_KEYTERM_MODELS = ("nova-3", "flux")


def _keyterm_kwargs(provider: str, model: str, keyterms: list[str]) -> dict[str, Any]:
    """The one `keyterms` field, in the provider's own dialect.

    Four providers, four spellings. Returns the settings kwarg to merge, or
    nothing at all when there are no keyterms to send — an empty list must not
    become an empty parameter.
    """
    if not keyterms:
        return {}
    if provider == "deepgram":
        if str(model).startswith(_DEEPGRAM_KEYTERM_MODELS):
            return {"keyterm": keyterms}
        return {"keywords": keyterms}
    if provider == "cartesia":
        # Honored by ink-2 and ink-preview only; Pipecat warns and drops them
        # for other models, and truncates past 100 terms / 1200 characters.
        return {"keyterm": keyterms}
    if provider == "openai":
        return {"keywords": keyterms}
    if provider == "elevenlabs":
        return {"keyterms": keyterms}
    return {}


# The STT mirror of `_AURA_DEFAULT` / `_TTS_DEFAULTS` below: one provider's
# model name sat in a provider-agnostic field as the default for all of them.
# TTS has resolved this since it was written; STT never did.
#
# `STTConfig.model` defaulted to `nova-3-general` — Deepgram's model — for
# every provider, and the `or "<default>"` fallbacks the branches carried
# could never fire against a truthy value. So an agent that named no model
# sent Deepgram's model name to whichever provider it had chosen, and all
# three others reject it outright:
#
#   openai      404 The model `nova-3-general` does not exist
#   elevenlabs  400 unsupported_model: 'nova-3-general' is not a valid model
#   cartesia    400 invalid model
#
# A dead STT stage: the caller is heard by nothing. The field's default is now
# empty, but `model_dump()` persisted the old one into every config_blob
# already written, so the legacy value is also treated as unset on the three
# providers that cannot serve it. Anything else the agent chose passes through
# untouched, and a wrong one is reported by the provider (adr/0016).
_NOVA_DEFAULT = "nova-3-general"
_STT_DEFAULTS: dict[str, str] = {
    "deepgram": _NOVA_DEFAULT,
    "openai": "gpt-transcribe",
    "elevenlabs": "scribe_v1",
    "cartesia": "ink-whisper",
}


def _stt_model(provider: str, configured: str) -> str:
    """The model to send, resolving an unset or legacy-sentinel value."""
    if configured and not (configured == _NOVA_DEFAULT and provider != "deepgram"):
        return configured
    return _STT_DEFAULTS.get(provider, configured)


def _create_stt_service(
    config: AgentConfig, openai_api_key: str, *, sample_rate: int = 8000
) -> Any:
    """Create STT service. Supports deepgram, openai, elevenlabs, and cartesia.

    `stt.extra` is forwarded to every provider. Pipecat's `ServiceSettings`
    treats it as overflow — `given_fields()` merges its entries at the top
    level — and Deepgram promotes a key matching a declared field onto that
    field. That promotion is how `profanity_filter` stays reachable now that
    Pipecat 1.9 no longer sends it by default.

    `stt.keyterms` wins over the same key in `extra`: whichever dialect the
    provider wants is filtered back out of the overflow, so a leftover
    `extra: {"keyterm": [...]}` cannot quietly override the managed field or
    reach a provider that would 400 on it.
    """
    provider = config.stt.provider
    model = _stt_model(provider, config.stt.model)
    keyterm_kwargs = _keyterm_kwargs(provider, model, config.stt.keyterms)

    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            sample_rate=sample_rate,
            encoding="linear16",
            settings=DeepgramSTTService.Settings(
                model=model,
                language=config.stt.language or "en",
                interim_results=True,
                punctuate=True,
                smart_format=True,
                **keyterm_kwargs,
                extra=_overflow(
                    config.stt.extra,
                    "model",
                    "language",
                    "interim_results",
                    "punctuate",
                    "smart_format",
                    *keyterm_kwargs,
                ),
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
                model=model,
                language=config.stt.language or "en",
                **keyterm_kwargs,
                extra=_overflow(config.stt.extra, "model", "language", *keyterm_kwargs),
            ),
        )
        stt._sample_rate = sample_rate
        return stt

    if provider == "openai":
        from pipecat.services.openai.stt import OpenAISTTService

        return OpenAISTTService(
            api_key=openai_api_key,
            settings=OpenAISTTService.Settings(
                model=model,
                **keyterm_kwargs,
                extra=_overflow(config.stt.extra, "model", *keyterm_kwargs),
            ),
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
                model=model,
                language=config.stt.language or "en",
                **keyterm_kwargs,
                extra=_overflow(config.stt.extra, "model", "language", *keyterm_kwargs),
            ),
        )
        stt._sample_rate = sample_rate
        return stt

    raise ValueError(f"Unsupported STT provider: {provider}")


def _validate_byom_url(base_url: str, byom_settings: BYOMSettings) -> None:
    """Validate base_url against BYOM allowlist settings."""
    if not byom_settings.enabled:
        raise ValueError("BYOM (custom LLM providers) is disabled")
    # The pattern check itself is shared with MCP server URLs, which are the
    # same class of attacker-influenceable outbound target.
    from turncall.services.url_allowlist import check_url_allowed

    check_url_allowed(base_url, byom_settings.allowed_url_patterns)


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
    system_instruction: str = "",
) -> Any:
    """Create LLM service. Supports openai, ollama, custom_openai, anthropic, openrouter.

    temperature/max_tokens default to the agent's llm config; pass explicit
    values to override (e.g. the voicemail classifier pins a low temperature).
    reasoning_effort defaults to the agent's config; pass explicit None to force
    it off (e.g. the deterministic voicemail classifier). OpenAI-family only.

    system_instruction is the agent's system prompt. It belongs on the service,
    not as a "system" message at the head of the LLMContext: Pipecat deprecated
    that in 1.9 and stops honouring it in 2.0.
    """
    provider = config.llm.provider
    temperature = temperature if temperature is not None else config.llm.temperature
    max_tokens = max_tokens if max_tokens is not None else config.llm.max_tokens
    effort = (
        config.llm.reasoning_effort
        if reasoning_effort is _USE_CONFIG
        else reasoning_effort
    )

    # Empty means unset — spread in only when there is one, so a service without
    # a prompt keeps the provider default instead of an empty string.
    si = {"system_instruction": system_instruction} if system_instruction else {}

    # One provider's model sat in a provider-agnostic field as the default for
    # all of them; see services/llm_models.py. Resolved once here so every
    # branch below — and `is_anthropic_model`, which reads the name to decide
    # whether Bedrock gets a temperature — sees the same value.
    model = resolve_llm_model(provider, config.llm.model)

    if provider == "openrouter":
        # OpenRouter is OpenAI-compatible; fallback_models ride in extra_body as
        # the `models` array (primary first), tried in order on rate-limit/error.
        # See ADR-0003. reasoning_effort folds into the same extra_body.
        models = (
            {"models": [model, *config.llm.fallback_models]}
            if config.llm.fallback_models
            else None
        )
        return OpenAILLMService(
            api_key=config.llm.api_key or openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            settings=OpenAILLMService.Settings(
                model=model,
                **si,
                extra=_openai_extra_body(effort, models),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "openai":
        return OpenAILLMService(
            api_key=openai_api_key,
            settings=OpenAILLMService.Settings(
                model=model,
                **si,
                extra=_openai_extra_body(effort),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        resolved_key = config.llm.api_key or anthropic_api_key
        # No temperature. Anthropic rejects it on current models — a
        # claude-sonnet-5 request carrying one answers 400 "`temperature` is
        # deprecated for this model.", which makes the service unusable and
        # ends the call on its first LLM turn. TurnCall's 0.7 is its own
        # default rather than a value the agent asked for, so it broke every
        # Anthropic call for a knob nobody set. The text path in llm_text.py
        # never sent one; this matches it. An older model that still accepts
        # temperature can be given one through `llm.extra`.
        anthropic_extra = _anthropic_extra_body(
            _overflow(config.llm.extra, "model", "max_tokens", "system_instruction")
        )
        return AnthropicLLMService(
            api_key=resolved_key,
            settings=AnthropicLLMService.Settings(
                model=model,
                **si,
                max_tokens=max_tokens,
                **({"extra": anthropic_extra} if anthropic_extra else {}),
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
                model=model,
                **si,
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
                model=model,
                **si,
                extra=_openai_extra_body(effort),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    if provider == "bedrock":
        from pipecat.services.aws.llm import AWSBedrockLLMService

        from turncall.services.aws_credentials import resolve_aws_credentials
        from turncall.services.bedrock_models import is_anthropic_model

        credentials = resolve_aws_credentials(config.aws)
        settings_kwargs: dict[str, Any] = {
            "model": model,
            **si,
            "max_tokens": max_tokens,
        }
        # No temperature for Claude on Bedrock, for the reason the direct
        # Anthropic branch above gives: current models answer 400
        # "`temperature` is deprecated for this model." and the call dies on
        # its first LLM turn. Bedrock is a gateway, so the deprecation is the
        # model's, not the endpoint's — but only Anthropic's models have it,
        # and Meta/Mistral/Amazon still want the value.
        if not is_anthropic_model(model):
            settings_kwargs["temperature"] = temperature
        if config.llm.extra:
            # Bedrock's passthrough for model-specific parameters — how
            # Anthropic extended thinking is reached here. reasoning_effort
            # stays OpenAI-family-only (ADR-0014) rather than growing a
            # second spelling for the same idea.
            settings_kwargs["additional_model_request_fields"] = config.llm.extra
        return AWSBedrockLLMService(
            settings=AWSBedrockLLMService.Settings(**settings_kwargs),
            **credentials.bedrock_kwargs(),
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


# `TTSConfig.model` and `.voice` both default to a Deepgram Aura voice whatever
# the provider is set to, so a Cartesia, OpenAI or ElevenLabs agent that never
# set them would send that provider an Aura name. Treat exactly that value as
# "never set" for the other three, and fall back to each provider's own.
_AURA_DEFAULT = "aura-2-helena-en"
_TTS_DEFAULTS: dict[str, tuple[str | None, str | None]] = {
    "deepgram": (_AURA_DEFAULT, _AURA_DEFAULT),
    "elevenlabs": ("eleven_flash_v2_5", "Rachel"),
    "openai": ("tts-1", "alloy"),
    # Cartesia voices are account-specific ids and Pipecat has no default
    # either, so there is nothing sensible to guess; an unset voice warns.
    "cartesia": ("sonic-3.6", None),
}


def _tts_model_voice(config: AgentConfig) -> tuple[str | None, str | None]:
    """The model and voice to send, with the cross-provider default undone."""
    provider = config.tts.provider
    model, voice = config.tts.model, config.tts.voice
    if provider != "deepgram":
        model = None if model == _AURA_DEFAULT else model
        voice = None if voice == _AURA_DEFAULT else voice
    default_model, default_voice = _TTS_DEFAULTS.get(provider, (None, None))
    return model or default_model, voice or default_voice


def _create_tts_service(config: AgentConfig, openai_api_key: str) -> Any:
    """Create TTS service. Supports deepgram, openai, elevenlabs, and cartesia."""
    provider = config.tts.provider

    # LLMs emit markdown (**bold**, `code`) that TTS would otherwise read aloud
    # literally ("asterisk asterisk"). Strip it on the way into every voice.
    # Pipecat wants (aggregation_type | "*", transform) tuples; "*" = all types.
    from pipecat.utils.text.transforms import strip_markdown

    text_transforms = [("*", strip_markdown)]
    speed = {"speed": config.tts.speed} if config.tts.speed != 1.0 else {}
    model, voice = _tts_model_voice(config)

    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        return DeepgramTTSService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            settings=DeepgramTTSService.Settings(
                voice=voice,
                **speed,
                extra=_overflow(config.tts.extra, "voice", "speed"),
            ),
            text_transforms=text_transforms,
        )

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        # ElevenLabs uses voice IDs (e.g. "21m00Tcm4TlvDq8ikWAM" for Rachel)
        # or voice names via Settings. Pass as voice in Settings.
        return ElevenLabsTTSService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(
                voice=voice,
                model=model,
                **speed,
                extra=_overflow(config.tts.extra, "voice", "model", "speed"),
            ),
            text_transforms=text_transforms,
        )

    if provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(
            api_key=openai_api_key,
            settings=OpenAITTSService.Settings(
                model=model,
                voice=voice,
                **speed,
                extra=_overflow(config.tts.extra, "model", "voice", "speed"),
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
        if not voice:
            # Cartesia will reject the request; say why here rather than leave
            # the provider's error as the only clue.
            logger.warning(
                "Cartesia TTS has no voice set; set tts.voice to a Cartesia voice id"
            )
        tts_settings = CartesiaTTSService.Settings(
            model=model,
            voice=voice,
            language=config.tts.extra.get("language", config.language),
            extra=_overflow(
                config.tts.extra, "model", "voice", "language", "emotion", "speed"
            ),
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


def _anthropic_extra_body(extra: dict[str, Any]) -> dict[str, Any]:
    """Route `llm.extra` keys the Anthropic SDK no longer accepts into
    `extra_body`, where they still reach the model.

    `messages.create()` dropped `temperature`, `top_k` and `top_p` from its
    signature; passing one as a keyword raises TypeError on the first LLM turn
    of the call. CLAUDE.md has been telling people to reach for `llm.extra` to
    set a temperature on a model that still accepts one — which crashed rather
    than worked. Anything the SDK will not take by name travels in `extra_body`
    instead, verified against its own signature so this survives the next SDK
    release. An explicit `extra_body` the caller wrote is merged, not replaced.
    """
    if not extra:
        return {}

    import inspect

    from anthropic.resources.messages import AsyncMessages

    accepted = set(inspect.signature(AsyncMessages.create).parameters)

    passthrough: dict[str, Any] = {}
    body: dict[str, Any] = dict(extra.get("extra_body") or {})
    for key, value in extra.items():
        if key == "extra_body":
            continue
        if key in accepted:
            passthrough[key] = value
        else:
            body[key] = value

    if body:
        passthrough["extra_body"] = body
    return passthrough


def _build_vad_analyzer(
    config: AgentConfig,
    *,
    sample_rate: int,
    pipecat_settings: Any | None = None,
    smart_turn: bool = False,
) -> Any:
    """Silero VAD, carrying the two settings that used to be decoration.

    `silence_timeout_ms` (200-5000, documented and in the OpenAPI spec) and
    `PIPECAT_VAD_CONFIDENCE_THRESHOLD` were both read by nothing: the analyzer
    was built bare and Pipecat's defaults decided how long a pause ends a turn
    and how sure the detector has to be. Built in one place so the cascade and
    S2S pipelines can't drift apart on it.

    With Smart Turn on, VAD is the *fast detector* and the model is the
    decider, so the VAD window is Pipecat's 0.2 rather than the agent's
    `silence_timeout_ms`. The two are otherwise serial, not parallel:
    `BaseSmartTurn.append_audio` is handed `is_speech=vad_user_speaking`, so
    its own silence counter does not start until VAD has already waited out
    its window. At our 800ms default that put end-of-turn 1.8s after the
    caller stopped talking (0.8 VAD + 1.0 `smart_turn_stop_secs`) before the
    LLM was even called. Pipecat warns about the same mismatch from the other
    end: a VAD window at or above the STT's p99 transcript latency (0.35s on
    Deepgram) collapses its turn-stop safety net to zero, leaving the
    aggregator's 5s `user_turn_stop_timeout` as the only thing ending some
    turns.

    Without Smart Turn nothing else decides the turn, so `silence_timeout_ms`
    keeps driving VAD there — including on S2S.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VAD_STOP_SECS, VADParams

    stop_secs = (
        VAD_STOP_SECS
        if smart_turn and config.smart_turn_detection
        else config.silence_timeout_ms / 1000
    )
    params: dict[str, Any] = {"stop_secs": stop_secs}
    confidence = getattr(pipecat_settings, "vad_confidence_threshold", None)
    if confidence is not None:
        params["confidence"] = confidence

    return SileroVADAnalyzer(sample_rate=sample_rate, params=VADParams(**params))


def _build_turn_strategies(config: AgentConfig, *, smart_turn: bool) -> Any | None:
    """The user's turn strategies: when a turn starts (barge-in) and when it
    ends (smart turn).

    Both land on one `UserTurnStrategies`, so they are built together —
    assigning it twice would drop whichever went first, and losing smart turn
    that way would be invisible.

    Returns None when the agent asked for neither, leaving Pipecat's defaults
    alone. `smart_turn=False` for S2S, which has no cascade turn analyzer.
    """
    strategies: dict[str, Any] = {}

    # Barge-in. `interruption_enabled: false` was accepted, validated and read
    # by nothing, so the caller could always talk over the agent whatever the
    # config said. Pipecat spells the control `enable_interruptions` on the
    # turn-start strategy: it decides whether speech mid-response broadcasts
    # an interruption.
    if not config.interruption_enabled:
        from pipecat.turns.user_start import (
            TranscriptionUserTurnStartStrategy,
            VADUserTurnStartStrategy,
        )

        # Both of Pipecat's defaults, not just the VAD one. `UserTurnStrategies`
        # fills `start` with the pair when given none, and supplying a list
        # replaces it wholesale — passing only the VAD strategy would turn off
        # barge-in and silently drop transcription-driven turn start with it.
        strategies["start"] = [
            VADUserTurnStartStrategy(enable_interruptions=False),
            TranscriptionUserTurnStartStrategy(enable_interruptions=False),
        ]
        logger.info("Barge-in disabled: the caller cannot interrupt the agent")

    if smart_turn and config.smart_turn_detection:
        try:
            from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
                LocalSmartTurnAnalyzerV3,
            )
            from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
                TurnAnalyzerUserTurnStopStrategy,
            )

            turn_analyzer = LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=config.smart_turn_stop_secs),
            )
            strategies["stop"] = [
                TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer)
            ]
            logger.info("Smart turn detection enabled (LocalSmartTurnV3)")
        except Exception:
            logger.warning("Smart turn detection unavailable, using VAD-only")

    if not strategies:
        return None

    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    return UserTurnStrategies(**strategies)


def _build_system_instruction(
    config: AgentConfig,
    *,
    inject_tools_prompt: bool = False,
    knowledge_preamble: str = "",
) -> str:
    """Compose the agent's system instruction.

    This goes on the LLM service as `system_instruction`, not into the
    LLMContext as a "system" message. Pipecat deprecated that in 1.9 and drops
    it in 2.0, and the OpenAI adapter prepends `system_instruction` to the
    context messages anyway — so doing both would send the prompt twice.

    The knowledge preamble leads: prompt-mode document text and the awareness
    hint for auto/tool KBs sit in front of the agent's own words, as before.
    """
    content = config.system_prompt or ""
    if inject_tools_prompt:
        content += _build_tools_prompt_section(config)
    content += _build_guardrails_section(config)
    if knowledge_preamble:
        return f"{knowledge_preamble}\n\n{content}" if content else knowledge_preamble
    return content


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

    # Deduplicate by name, first registration wins. Providers reject a tools
    # array with two identical function names, and two MCP servers exposing a
    # common one ("search", "get") is ordinary rather than exotic. Static
    # tools come first, so a discovered tool can never take over a name the
    # agent config already uses.
    seen: set[str] = set()
    deduped = []
    for tool in all_tools:
        if tool.name in seen:
            logger.warning("tool_name_collision_skipped", tool=tool.name)
            continue
        seen.add(tool.name)
        deduped.append(tool)
    all_tools = deduped

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
            logger.warning(
                "Avatar enabled but HEYGEN_LIVE_AVATAR_API_KEY unset; skipping"
            )
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
            mcp_tools=mcp_tools,
        )

    # --- Cascade pipeline (STT → LLM → TTS) ---
    # Create AI services
    stt = _create_stt_service(config, openai_api_key, sample_rate=audio_sample_rate)

    # BYOM providers describe their tools in the prompt rather than advertising
    # them through the API: many local models (Gemma and friends) only do
    # function calling by prompting.
    byom_provider = config.llm.provider in ("ollama", "custom_openai")
    system_instruction = _build_system_instruction(
        config,
        inject_tools_prompt=byom_provider,
        knowledge_preamble=knowledge_preamble,
    )
    if byom_provider and config.tools:
        logger.info(
            "BYOM mode: tools injected into system prompt for {provider}/{model}",
            provider=config.llm.provider,
            model=config.llm.model,
        )

    llm = _create_llm_service(
        config,
        openai_api_key,
        anthropic_api_key=anthropic_api_key,
        openrouter_api_key=openrouter_api_key,
        byom_settings=byom_settings,
        system_instruction=system_instruction,
    )
    tts = _create_tts_service(config, openai_api_key)

    # The context starts empty — the system prompt is on the LLM service as
    # system_instruction. BYOM providers advertise no API-level tools, theirs
    # being described in the prompt instead.
    kwargs: dict[str, Any] = {"messages": []}
    if not byom_provider:
        tools_schema = _build_tools_schema(config, extra_tools=mcp_tools)
        if tools_schema is not None:
            kwargs["tools"] = tools_schema

    context = LLMContext(**kwargs)

    # VAD + turn detection configuration (Pipecat 1.0: VAD lives on the user aggregator)
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregatorParams,
    )

    user_params_kwargs: dict[str, Any] = {
        "vad_analyzer": _build_vad_analyzer(
            config,
            sample_rate=audio_sample_rate,
            pipecat_settings=pipecat_settings,
            smart_turn=True,
        ),
        # Pipecat counts in seconds; 0 disables, which is also our "off".
        # CallSession registers the handler that reacts to it.
        "user_idle_timeout": config.user_idle_timeout_ms / 1000,
    }

    turn_strategies = _build_turn_strategies(config, smart_turn=True)
    if turn_strategies is not None:
        user_params_kwargs["user_turn_strategies"] = turn_strategies

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
    assistant_tap = AssistantTranscriptTapProcessor(
        call_context=call_context, llm_service=llm
    )
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

    recorder = attach_recorder(transport, call_context, sample_rate=audio_sample_rate)

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

    # The resolved model, not the configured one: an agent that named none
    # runs something else entirely, and a log that says otherwise sends the
    # next person looking in the wrong place. Safe to resolve again here — the
    # service was already built from it, so it cannot raise now.
    llm_info = f"{config.llm.provider}/{resolve_llm_model(config.llm.provider, config.llm.model)}"
    if config.llm.base_url:
        llm_info += f" @ {config.llm.base_url}"
    logger.info(
        "Pipeline created: STT={stt}/{stt_model} LLM={llm_info} TTS={tts}/{tts_voice}",
        stt=config.stt.provider,
        stt_model=_stt_model(config.stt.provider, config.stt.model),
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
    mcp_tools: list[Any] | None = None,
) -> Pipeline:
    """Build a speech-to-speech pipeline using OpenAI Realtime or Gemini Live.

    The S2S model handles STT + LLM + TTS in a single WebSocket connection.
    Pipeline is much simpler than cascade:
      transport.input → [VAD] → S2S_LLM → transport.output → context_agg → observability
    """
    if not config.interruption_enabled:
        # Cascade only, deliberately. Supplying `user_turn_strategies` here
        # would discard what realtime_service_mode auto-swaps in for OpenAI
        # Realtime and Gemini Live — Pipecat replaces the whole set rather than
        # merging — and on server-side turn detection the provider owns
        # turn-taking outright. Better said than silently half-applied.
        logger.warning(
            "interruption_enabled=false is not applied on S2S (turn_detection="
            "'{td}'): the realtime service owns turn-taking. Use "
            "pipeline_mode='cascade' to disable barge-in.",
            td=config.s2s.turn_detection,
        )

    from turncall.orchestrator.s2s_config import create_s2s_service

    # A gateway base_url is an attacker-influenceable outbound target — gate it
    # through the same BYOM allowlist as custom text-LLM endpoints.
    if config.s2s.base_url and byom_settings:
        _validate_byom_url(config.s2s.base_url, byom_settings)

    s2s_llm = create_s2s_service(config, openai_api_key, google_api_key=google_api_key)

    # Build context — system prompt + first_message are handled by the S2S
    # service directly (SessionProperties.instructions for OpenAI,
    # system_instruction for Gemini). Only pass tools via context.
    tools_schema = _build_tools_schema(config, extra_tools=mcp_tools)
    context_kwargs: dict[str, Any] = {"messages": []}
    if tools_schema is not None:
        context_kwargs["tools"] = tools_schema
    context = LLMContext(**context_kwargs)

    # Pipecat 1.0: VAD lives on the user aggregator (needed for pipecat_vad turn detection)
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregatorParams,
    )

    # Built unconditionally, where it used to be built only for pipecat_vad:
    # the idle guard is carried by these params, and `server_vad` is the
    # default, so leaving them None there would have quietly disabled it on
    # most S2S agents. Passing None only meant `LLMUserAggregatorParams()`, so
    # supplying one with no analyzer is the same thing plus the timeout.
    s2s_user_params = LLMUserAggregatorParams(
        user_idle_timeout=config.user_idle_timeout_ms / 1000,
        **(
            {"vad_analyzer": _build_vad_analyzer(config, sample_rate=audio_sample_rate)}
            if config.s2s.turn_detection == "pipecat_vad"
            else {}
        ),
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

    recorder = attach_recorder(transport, call_context, sample_rate=audio_sample_rate)

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
