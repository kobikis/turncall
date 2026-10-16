"""Config-driven Pipecat pipeline construction.

Reads an AgentConfig from the database and builds a fully-wired
Pipecat pipeline with the appropriate STT, LLM, TTS services,
context aggregation, and observability.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from openai import Timeout
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.openai.llm import OpenAILLMService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.config.settings import BYOMSettings, PipecatSettings
from turncall.domain.models import (
    AgentConfig,
    LLMConfig,
    resolve_delegation,
)
from turncall.orchestrator.deepgram_flux import (
    MIXED_FLUX_GROUP,
    create_flux_stt,
    flux_turn_strategies,
    is_eager,
    is_flux,
    mixes_flux,
)
from turncall.orchestrator.knowledge_processor import KnowledgeTool, knowledge_tool
from turncall.orchestrator.observability import ObservabilityProcessor
from turncall.services.credentials import Credentials
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
    # None for a conversation with no platform behind it — the website demo
    # (`turncall.demo`): no database, no Redis, no project. See `records`.
    session_factory: async_sessionmaker[AsyncSession] | None
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

    @property
    def has_database(self) -> bool:
        return self.session_factory is not None

    @property
    def records(self) -> bool:
        """Whether there is a `calls` row to write to: status, call events,
        tool invocations, the recording, the control channel, `call.ended`.

        Neither an eval (ADR-0018) nor the demo has one. Every call-scoped
        side effect asks this; `is_eval` is left for what an eval does
        differently on purpose — its RTVI bridge, no hangup gate, no ambience.
        """
        return self.has_database and not self.is_eval


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

# AssemblyAI refuses a session carrying more than 100 keyterms instead of using
# some of them, and pipecat sends the list as given: one term over is a dead
# STT stage, not a weaker one. The first 100 are kept, in the order the agent
# lists them (the API boundary has already dropped blanks and duplicates).
_ASSEMBLYAI_MAX_KEYTERMS = 100


def _keyterm_kwargs(provider: str, model: str, keyterms: list[str]) -> dict[str, Any]:
    """The one `keyterms` field, in the provider's own dialect.

    Five providers, five spellings. Returns the settings kwarg to merge, or
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
    if provider == "assemblyai":
        if len(keyterms) > _ASSEMBLYAI_MAX_KEYTERMS:
            logger.warning(
                "assemblyai_keyterms_truncated count={count} kept={kept}",
                count=len(keyterms),
                kept=_ASSEMBLYAI_MAX_KEYTERMS,
            )
        return {"keyterms_prompt": keyterms[:_ASSEMBLYAI_MAX_KEYTERMS]}
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
    # Universal-Streaming, $0.15/hr. Not pipecat's default (Universal-3.5 Pro,
    # three times the price): the cheaper model is the reason to pick this
    # provider, and Pro stays one `stt.model` away.
    "assemblyai": "universal-streaming-english",
}

# AssemblyAI splits its streaming models by language rather than taking one as
# a parameter, and the English model given Spanish transcribes it as English
# words with no error to say so. Its multilingual sibling costs the same and
# covers en/es/de/fr/pt/it; anything wider is Universal-3 Pro, named explicitly.
_ASSEMBLYAI_MULTILINGUAL = "universal-streaming-multilingual"


def _stt_model(provider: str, configured: str, language: str | None = "en") -> str:
    """The model to send, resolving an unset or legacy-sentinel value."""
    if configured and not (configured == _NOVA_DEFAULT and provider != "deepgram"):
        return configured
    if provider == "assemblyai" and not (language or "en").lower().startswith("en"):
        return _ASSEMBLYAI_MULTILINGUAL
    return _STT_DEFAULTS.get(provider, configured)


def _flux_owns_turn(config: AgentConfig) -> bool:
    """Whether the agent's STT decides when the caller's turn is over (#204)."""
    return config.stt.provider == "deepgram" and is_flux(
        _stt_model("deepgram", config.stt.model)
    )


def _create_stt_service(
    config: AgentConfig, credentials: Credentials, *, sample_rate: int = 8000
) -> Any:
    """Create STT service: deepgram, openai, elevenlabs, cartesia, assemblyai.

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
    model = _stt_model(provider, config.stt.model, config.stt.language)
    keyterm_kwargs = _keyterm_kwargs(provider, model, config.stt.keyterms)

    if provider == "deepgram" and is_flux(model):
        return create_flux_stt(
            api_key=credentials.api_key("deepgram"),
            settings={
                **_overflow(config.stt.extra, "model", *keyterm_kwargs),
                "model": model,
                **keyterm_kwargs,
            },
            sample_rate=sample_rate,
            interruptions=config.interruption_enabled,
        )

    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(
            api_key=credentials.api_key("deepgram"),
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
            api_key=credentials.api_key("elevenlabs"),
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
            api_key=credentials.api_key("openai"),
            settings=OpenAISTTService.Settings(
                model=model,
                **keyterm_kwargs,
                extra=_overflow(config.stt.extra, "model", *keyterm_kwargs),
            ),
        )

    if provider == "cartesia":
        from pipecat.services.cartesia.stt import CartesiaSTTService

        api_key = credentials.api_key("cartesia")
        if not api_key:
            raise ValueError(
                "No Cartesia key: connect a project credential or set "
                "CARTESIA_API_KEY (required for Cartesia STT)"
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

    if provider == "assemblyai":
        return _create_assemblyai_stt(
            config.stt.extra,
            model=model,
            keyterm_kwargs=keyterm_kwargs,
            api_key=credentials.api_key("assemblyai"),
            sample_rate=sample_rate,
        )

    raise ValueError(f"Unsupported STT provider: {provider}")


def _create_assemblyai_stt(
    extra: dict[str, Any],
    *,
    model: str,
    keyterm_kwargs: dict[str, Any],
    api_key: str,
    sample_rate: int,
) -> Any:
    """AssemblyAI Universal-Streaming, in pipecat's own turn mode.

    Pipecat builds the connection URL from its declared settings alone and
    never reads `extra`, so `stt.extra` is promoted onto them by name —
    `domain`, `mode`, `voice_focus`, `min_turn_silence` — or it would reach
    nothing. A key with no such setting is warned about rather than dropped
    in silence. `vad_force_turn_endpoint` stays on: TurnCall's VAD and Smart
    Turn decide when the caller is done, and AssemblyAI returns finals at once.
    """
    from pipecat.services.assemblyai.stt import AssemblyAISTTService

    settings = AssemblyAISTTService.Settings.from_mapping(
        {
            **_overflow(extra, "model", *keyterm_kwargs),
            "model": model,
            **keyterm_kwargs,
        }
    )
    if settings.extra:
        logger.warning(
            "assemblyai_stt_extra_ignored keys={keys}", keys=sorted(settings.extra)
        )
    return AssemblyAISTTService(
        api_key=api_key, sample_rate=sample_rate, settings=settings
    )


def _create_stt_stage(
    config: AgentConfig, credentials: Credentials, *, sample_rate: int
) -> Any:
    """The call's STT: the service itself, or it and its backups (#223). Each
    entry is built as `stt` would be, so its model resolves on its own terms.

    A group mixing Flux with other models is refused here as well as at the
    API: a call-init inline agent reaches the build without passing the API's
    schema, and the call then fails at build, saying why, rather than leaving
    every turn after a switch on a 5s timeout."""
    if mixes_flux(config.stt):
        raise ValueError(MIXED_FLUX_GROUP)
    stt = _create_stt_service(config, credentials, sample_rate=sample_rate)
    if not config.stt.failover:
        return stt
    from turncall.orchestrator.failover import FailoverSTT

    members = [(_stt_label(config.stt), stt)]
    for entry in config.stt.failover:
        backup = config.model_copy(update={"stt": entry})
        service = _create_stt_service(backup, credentials, sample_rate=sample_rate)
        members.append((_stt_label(entry), service))
    return FailoverSTT(members)


def _stt_label(stt: Any) -> str:
    return f"{stt.provider}/{_stt_model(stt.provider, stt.model, stt.language)}"


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
    credentials: Credentials,
    *,
    byom_settings: BYOMSettings | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = _USE_CONFIG,
    system_instruction: str = "",
) -> Any:
    """Create LLM service. Supports openai, ollama, custom_openai, anthropic,
    openrouter, bedrock, google.

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
            api_key=credentials.api_key("openrouter", config.llm.api_key),
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
            api_key=credentials.api_key("openai", config.llm.api_key),
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

        resolved_key = credentials.api_key("anthropic", config.llm.api_key)
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

    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        from turncall.services.gemini_models import takes_temperature

        google_settings: dict[str, Any] = {
            "model": model,
            **si,
            "max_tokens": max_tokens,
        }
        # No temperature for Gemini 3, which Google says loops below its own
        # 1.0 — and 0.7 is TurnCall's default, not the agent's choice. That
        # includes the voicemail classifier's pin. services/gemini_models.py.
        if takes_temperature(model):
            google_settings["temperature"] = temperature
        if config.llm.extra:
            # Merged into the generation config last, so it can set a
            # temperature on Gemini 3 anyway, or a thinking level.
            google_settings["extra"] = dict(config.llm.extra)
        return GoogleLLMService(
            api_key=credentials.api_key("google", config.llm.api_key),
            settings=GoogleLLMService.Settings(**google_settings),
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
            # No account: the key belongs to whatever endpoint base_url names,
            # so only the agent's own can apply (domain/providers.py).
            api_key=credentials.api_key("custom_openai", config.llm.api_key)
            or "no-key",
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

        aws = resolve_aws_credentials(credentials.aws(config.aws))
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
            **aws.bedrock_kwargs(),
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


def _llm_label(llm: LLMConfig) -> str:
    return f"{llm.provider}/{resolve_llm_model(llm.provider, llm.model)}"


def _create_llm_stage(
    config: AgentConfig, credentials: Credentials, **kwargs: Any
) -> Any:
    """The call's LLM: the service itself, or it and its backups (#222)."""
    llm = _create_llm_service(config, credentials, **kwargs)
    if not config.llm.failover:
        return llm
    from turncall.orchestrator.llm_failover import FailoverLLM

    members = [(_llm_label(config.llm), llm)]
    for entry in config.llm.failover:
        backup = config.model_copy(update={"llm": entry})
        members.append(
            (_llm_label(entry), _create_llm_service(backup, credentials, **kwargs))
        )
    return FailoverLLM(members)


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
    elif voice and not voice.startswith("aura"):
        # Deepgram's voice *is* its model, and every one is `aura-*`. Anything
        # else — "alloy", left over from switching off OpenAI — is refused with
        # an HTTP 400 at connect, which ends the call before it says a word.
        logger.warning(f"tts_voice_not_deepgram: {voice!r}, using the model instead")
        voice = model if model and model.startswith("aura") else None
    default_model, default_voice = _TTS_DEFAULTS.get(provider, (None, None))
    return model or default_model, voice or default_voice


def _tts_text_transforms(
    service_cls: Any, config: AgentConfig, *, pronunciations: bool = True
) -> list[tuple[str, Any]]:
    """The text transforms for one provider's TTS service class.

    LLMs emit markdown (**bold**, `code`) that TTS would otherwise read aloud
    literally ("asterisk asterisk"). Stripping it is provider-independent and
    comes first for everyone. Pipecat wants
    (aggregation_type | "*", transform) tuples; "*" = all types.

    Composed per service class, not once and shared, because the pronunciation
    transform is not provider-independent: pipecat exposes it as a
    **classmethod on the TTS service class**, each provider generating its own
    markup from the same IPA — Cartesia's inline phoneme blocks, ElevenLabs'
    SSML phoneme tags, Deepgram's inline pronunciation object. One shared list
    cannot carry that (#137, #140).

    The pronunciation transform is registered **last**. Transforms run in order
    and a later one rewrites what an earlier one inserted — markdown stripping
    would eat an SSML tag.

    A fresh list each call: a provider appending its own transform must not
    reach into another's. `test_tts_text_transforms.py` guards both halves.

    `pronunciations=False` withholds it for a service that will not read the
    markup. Measured on ElevenLabs' WebSocket with SSML parsing off, a
    `<phoneme>` tag is **spoken** — "phoneme alphabet i p a", at 3.5-4.3x the
    audio of the plain sentence. Markup a service ignores would be harmless;
    markup it reads aloud is worse than the mispronunciation it was meant to
    fix, so "spoken as written" has to mean the tag never goes (#147).
    """
    from pipecat.utils.text.transforms import strip_markdown

    transforms: list[tuple[str, Any]] = [("*", strip_markdown)]
    if pronunciations and config.tts.pronunciations:
        transforms.append(
            ("*", service_cls.pronunciation_transform_ipa(config.tts.pronunciations))
        )
    return transforms


def _warn_unusable_pronunciations(config: AgentConfig, tts: Any) -> None:
    """Warn when the built voice cannot read the pronunciation markup.

    Whether a service reads it is a property of the *constructed* service, not
    of the provider name — on ElevenLabs it takes both a phoneme-capable model
    and `enable_ssml_parsing`, and OpenAI has no markup at all. So this is a
    warning where a service exists, never a refusal at create: an agent's voice
    can change long after its pronunciations were written, and the words are
    spoken as written either way. Same precedent as `interruption_enabled` on
    an S2S agent — said out loud rather than half-applied.
    """
    pronunciations = config.tts.pronunciations
    if not pronunciations:
        return

    # Asked of the service, one word at a time, exactly as pipecat's own
    # transform asks it: a formatter returning None is the only honest signal
    # that a word cannot be rendered, and it covers both a provider with no
    # markup at all (OpenAI) and a word a provider will not take.
    unusable = [
        word
        for word, ipa in pronunciations.items()
        if tts.format_pronunciation(word, ipa) is None
    ]
    if not unusable and tts.supports_pronunciations:
        return

    # The *resolved* model, not `config.tts.model`: that field's default is
    # Deepgram's, so an agent that named none would be told its ElevenLabs voice
    # is `aura-2-helena-en` — a diagnostic pointing at a model the service is
    # not running is worse than none, and the model is half of why ElevenLabs
    # refuses.
    logger.warning(
        "tts.pronunciations: {provider} ({model}) cannot read pronunciation "
        "markup with these settings, so these are spoken as written: {words}",
        provider=config.tts.provider,
        model=_tts_model_voice(config)[0],
        words=", ".join(unusable or pronunciations),
    )


# The OpenAI SDK's default is 600s, retried twice. A request OpenAI never
# answers (gpt-4o-mini-tts did, for hours, 2026-10-12) then held the call's
# session — and its live-call slot — open up to half an hour after the caller
# hung up, since the pipeline cannot end while it waits. The read timeout is
# between chunks, so a long reply streaming steadily is never cut.
# openai's own `Timeout`: its client is not plain httpx's, and reads an httpx
# `Timeout` as a number.
_OPENAI_TTS_TIMEOUT = Timeout(10.0, connect=5.0)


def _create_tts_service(
    config: AgentConfig, credentials: Credentials, **kwargs: Any
) -> Any:
    """Create TTS service. Supports deepgram, openai, elevenlabs, and cartesia.
    `kwargs` reach pipecat's `TTSService` as they are, beside the filter that
    keeps the model's stage directions unspoken."""
    from turncall.orchestrator.stage_directions import StageDirectionFilter

    provider = config.tts.provider
    kwargs = {
        "text_filters": [StageDirectionFilter(voice_tags=provider == "cartesia")],
        **kwargs,
    }

    speed = {"speed": config.tts.speed} if config.tts.speed != 1.0 else {}
    model, voice = _tts_model_voice(config)

    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        return DeepgramTTSService(
            api_key=credentials.api_key("deepgram"),
            settings=DeepgramTTSService.Settings(
                voice=voice,
                **speed,
                extra=_overflow(config.tts.extra, "voice", "speed"),
            ),
            text_transforms=_tts_text_transforms(DeepgramTTSService, config),
            **kwargs,
        )

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import (
            ELEVENLABS_PHONEME_MODELS,
            ElevenLabsTTSService,
        )

        # ElevenLabs reads SSML <phoneme> tags only with `enable_ssml_parsing`
        # on — a *constructor* kwarg, not a `Settings` field, so an author's
        # `tts.extra` lands in `Settings.extra` and can never reach it (#147).
        # TurnCall decides instead, and only for an agent that asked for
        # pronunciations on a model that can read them: SSML parsing changes how
        # **all** of that agent's text is read (a `<` in an LLM reply becomes
        # markup) and adds latency, so it is not a default. None rather than
        # False leaves the URL parameter off entirely, as before.
        #
        # The model half stays the author's: downgrading `eleven_flash_v2_5` to
        # `eleven_flash_v2` would trade what every caller hears for what one
        # might — `_warn_unusable_pronunciations` naming the resolved model is
        # the nudge. The set is pipecat's, never a copy; a bump that moves it is
        # a red build in `test_tts_pronunciations.py`, naming the version. It is
        # also measured-correct rather than conservative: on `eleven_flash_v2_5`
        # a phoneme tag is *dropped* with SSML parsing on (a long IPA returns
        # 0.78x the plain sentence's audio, where `eleven_flash_v2` returns
        # 2.44x) and recited aloud with it off. Adding v2_5 would lose the word.
        enable_ssml_parsing = (
            True
            if config.tts.pronunciations and model in ELEVENLABS_PHONEME_MODELS
            else None
        )

        # ElevenLabs uses voice IDs (e.g. "21m00Tcm4TlvDq8ikWAM" for Rachel)
        # or voice names via Settings. Pass as voice in Settings.
        return ElevenLabsTTSService(
            api_key=credentials.api_key("elevenlabs"),
            enable_ssml_parsing=enable_ssml_parsing,
            settings=ElevenLabsTTSService.Settings(
                voice=voice,
                model=model,
                **speed,
                extra=_overflow(config.tts.extra, "voice", "model", "speed"),
            ),
            text_transforms=_tts_text_transforms(
                ElevenLabsTTSService, config, pronunciations=bool(enable_ssml_parsing)
            ),
            **kwargs,
        )

    if provider == "openai":
        from openai import DefaultAsyncHttpxClient
        from pipecat.services.openai.tts import OpenAITTSService

        return OpenAITTSService(
            api_key=credentials.api_key("openai"),
            http_client=DefaultAsyncHttpxClient(timeout=_OPENAI_TTS_TIMEOUT),
            settings=OpenAITTSService.Settings(
                model=model,
                voice=voice,
                **speed,
                extra=_overflow(config.tts.extra, "model", "voice", "speed"),
            ),
            text_transforms=_tts_text_transforms(OpenAITTSService, config),
            **kwargs,
        )

    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        api_key = credentials.api_key("cartesia")
        if not api_key:
            raise ValueError(
                "No Cartesia key: connect a project credential or set "
                "CARTESIA_API_KEY (required for Cartesia TTS)"
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
            text_transforms=_tts_text_transforms(CartesiaTTSService, config),
            **kwargs,
        )

    raise ValueError(f"Unsupported TTS provider: {provider}")


def _create_tts_stage(config: AgentConfig, credentials: Credentials) -> Any:
    """The call's TTS: the service itself, or it and its backups (#223). Each
    entry is a whole TTS config, its pronunciations included."""
    # With a backup to move to, one reply that ends in silence writes a TTS
    # off: the caller has already heard 3s of nothing, and pipecat's default of
    # three in a row outlasts most callers. The last member keeps the default —
    # writing it off leaves nobody to speak, and ends the call.
    write_off = {"max_consecutive_zero_audio_contexts": 1}
    last = len(config.tts.failover)
    tts = _create_tts_service(config, credentials, **(write_off if last else {}))
    _warn_unusable_pronunciations(config, tts)
    if not config.tts.failover:
        return tts
    from turncall.orchestrator.failover import FailoverTTS

    members = [(_tts_label(config), tts)]
    for i, entry in enumerate(config.tts.failover, start=1):
        backup = config.model_copy(update={"tts": entry})
        service = _create_tts_service(
            backup, credentials, **(write_off if i < last else {})
        )
        _warn_unusable_pronunciations(backup, service)
        members.append((_tts_label(backup), service))
    return FailoverTTS(members)


def _user_mute_strategies(config: AgentConfig) -> list[Any]:
    """`greeting_interruptible: false` — the caller is muted until the first
    reply has played. pipecat's strategy lifts the mute if that reply fails
    before any audio, so a dead voice does not leave the caller unheard; a TTS
    failover absorbs the failure and the backup's greeting lifts it instead."""
    if config.greeting_interruptible:
        return []
    from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
        MuteUntilFirstBotCompleteUserMuteStrategy,
    )

    return [MuteUntilFirstBotCompleteUserMuteStrategy()]


def _tts_label(config: AgentConfig) -> str:
    model, voice = _tts_model_voice(config)
    # Deepgram's voice is what it runs; its `model` field is never sent.
    return (
        f"{config.tts.provider}/{voice if config.tts.provider == 'deepgram' else model}"
    )


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
    keeps driving VAD there — including on S2S. Unless the STT is Deepgram
    Flux, which decides the turn itself (#204): VAD stays at 0.2 then, deciding
    nothing, as the point the latency observer measures from.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VAD_STOP_SECS, VADParams

    decided_elsewhere = smart_turn and (
        config.smart_turn_detection or _flux_owns_turn(config)
    )
    stop_secs = VAD_STOP_SECS if decided_elsewhere else config.silence_timeout_ms / 1000
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

    A Deepgram Flux agent gets Flux's strategies instead, whatever
    `smart_turn_detection` says: Flux decides the turn, and a Smart Turn stop
    strategy beside it would only add a second wait after Flux's (#204).
    """
    if smart_turn and _flux_owns_turn(config):
        return flux_turn_strategies(
            interruptions=config.interruption_enabled,
            eager=is_eager(config.stt.extra),
        )

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
            from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
                TurnAnalyzerUserTurnStopStrategy,
            )

            from turncall.orchestrator.turn_detection import SharedSmartTurnAnalyzer

            # One ONNX session per process; the analyzer itself is the call's.
            turn_analyzer = SharedSmartTurnAnalyzer(
                params=SmartTurnParams(stop_secs=config.smart_turn_stop_secs),
            )
            strategies["stop"] = [
                TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer)
            ]
            logger.info("Smart turn detection enabled (LocalSmartTurnV3)")
        except Exception:
            logger.warning("Smart turn detection unavailable, using VAD-only")
            # VAD was built at its 0.2s window for the model; make up the rest.
            from turncall.orchestrator.turn_detection import (
                VAD_STOP_SECS,
                vad_turn_stop,
            )

            strategies["stop"] = [
                vad_turn_stop(config.silence_timeout_ms / 1000 - VAD_STOP_SECS)
            ]
    elif smart_turn:
        # Explicitly, never left to pipecat: its default stop strategy *is*
        # Smart Turn V3, so "off" used to mean Smart Turn anyway, waited on
        # after VAD's own `silence_timeout_ms` window (#269).
        from turncall.orchestrator.turn_detection import vad_turn_stop

        strategies["stop"] = [vad_turn_stop()]

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


def _advertised_tools(
    config: AgentConfig, extra_tools: list[Any] | None = None
) -> list[Any]:
    """Static + discovered tools, deduplicated by name, first one wins.

    Providers reject a tools array with two identical function names, and two
    MCP servers exposing a common one ("search", "get") is ordinary rather
    than exotic. Static tools come first, so a discovered tool can never take
    over a name the agent config already uses.
    """
    seen: set[str] = set()
    deduped = []
    for tool in [*config.tools, *(extra_tools or [])]:
        if tool.name in seen:
            logger.warning("tool_name_collision_skipped", tool=tool.name)
            continue
        seen.add(tool.name)
        deduped.append(tool)
    return deduped


def _with_knowledge(
    tools: list[Any] | None, knowledge: KnowledgeTool | None
) -> list[Any] | None:
    """Discovered tools plus `query_knowledge`, last — a name the agent's own
    tools already use stays theirs."""
    if knowledge is None:
        return tools
    return [*(tools or []), knowledge.definition]


def _build_tools_schema(
    config: AgentConfig,
    extra_tools: list[Any] | None = None,
) -> Any:
    """Convert tool definitions to Pipecat ToolsSchema.

    Args:
        config: Agent config with static tool definitions.
        extra_tools: Additional ToolDefinition objects (e.g. from MCP discovery).
    """
    all_tools = _advertised_tools(config, extra_tools)
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


def _create_avatar_service(avatar: Any, credentials: Credentials) -> Any:
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

        key = credentials.api_key("heygen")
        if not key:
            logger.warning(
                "Avatar enabled but no HeyGen key (project credential or "
                "HEYGEN_LIVE_AVATAR_API_KEY); skipping"
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

        key = credentials.api_key("tavus")
        if not key:
            logger.warning(
                "Avatar enabled but no Tavus key (project credential or "
                "TAVUS_API_KEY); skipping"
            )
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


# --- The RTVI server half, eval path only (evals-design §9.7) ---
#
# Pipecat's eval harness is an RTVI *client*. The server half is not the
# transport: it is an `RTVIProcessor` in the bot's pipeline plus an
# `RTVIObserver` on the bot's task. Without the pair, three things fail in the
# order they bite: nothing ever sends `bot-ready`, so every iteration dies at
# `handshake()`; nothing interprets the `send-text`/`raw-audio`/`dtmf` messages
# the eval serializer produces, so the caller's turn never reaches the LLM; and
# nothing emits `llm_response`/`function_call`, so `events_seen` stays empty
# even where the agent worked perfectly.
#
# The failure is silent by construction — a harness timeout scores `errored`,
# which is deliberately kept out of every pass/fail rate, so a total outage
# reads as "no signal" rather than as a red build.
#
# The two functions below are one decision. Dropping either leaves the same
# symptom as dropping both; `tests/unit/test_eval_rtvi_bridge.py` asserts both.


def _hangup_gate(call_context: CallContext) -> list[Any]:
    """`HangupAfterReply`, directly after the LLM: ends the call after the
    goodbye an ending tool asked for (#240). None in an eval, whose tools never
    arm it and whose length the harness owns."""
    if call_context.is_eval:
        return []
    from turncall.orchestrator.hangup import HangupAfterReply

    return [HangupAfterReply()]


def _speech_before_ambience(
    config: AgentConfig, call_context: CallContext
) -> list[Any]:
    """`SpeechBeforeAmbience`, directly before the output, where ambience
    plays (#265): never in an eval, which mixes no ambience."""
    if config.ambience is None or call_context.is_eval:
        return []
    from turncall.orchestrator.ambience import SpeechBeforeAmbience

    return [SpeechBeforeAmbience()]


def _eval_rtvi(call_context: CallContext) -> list[Any]:
    """The `RTVIProcessor`, for an eval pipeline's processor list.

    Placed directly after `transport.input()` so it sees the
    `InputTransportMessageFrame`s the eval serializer produces, and present in
    the list from the start rather than added from an `on_client_connected`
    handler: `client-ready` arrives as soon as the harness connects, and a
    processor that is not there yet cannot answer it.

    Empty on every call path, so no live call gains a processor.
    """
    if not call_context.is_eval:
        return []

    from pipecat.processors.frameworks.rtvi import RTVIProcessor

    return [RTVIProcessor()]


def eval_rtvi_observers(pipeline: Pipeline) -> list[Any]:
    """The `RTVIObserver` half of `_eval_rtvi`, for the pipeline task.

    Read back off the pipeline rather than threaded through
    `create_pipeline`'s return type — the same way `build_call_pipeline`
    already finds the `LLMService` to register tools on. Empty for a pipeline
    that has no RTVI processor, which is every live call.

    Not telemetry: the harness's `handshake()` sends an
    `RTVIConfigureObserverFrame` to raise the function-call report level, so
    the observer must exist to be configured, whatever
    `PIPECAT_ENABLE_OBSERVERS` says.
    """
    from pipecat.processors.frameworks.rtvi import RTVIProcessor

    return [
        processor.create_rtvi_observer()
        for processor in pipeline.processors
        if isinstance(processor, RTVIProcessor)
    ]


def create_pipeline(
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    credentials: Credentials,
    pipecat_settings: PipecatSettings,
    *,
    audio_sample_rate: int = 8000,
    byom_settings: BYOMSettings | None = None,
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
            credentials,
            pipecat_settings,
            audio_sample_rate=audio_sample_rate,
            byom_settings=byom_settings,
            knowledge_base_attachments=knowledge_base_attachments,
            knowledge_preamble=knowledge_preamble,
            mcp_tools=mcp_tools,
        )

    # --- Cascade pipeline (STT → LLM → TTS) ---
    # Create AI services
    stt = _create_stt_stage(config, credentials, sample_rate=audio_sample_rate)

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

    llm = _create_llm_stage(
        config,
        credentials,
        byom_settings=byom_settings,
        system_instruction=system_instruction,
    )
    tts = _create_tts_stage(config, credentials)

    # query_knowledge (tool mode) is registered here, before pipeline_builder
    # registers static + MCP tools — so on a name clash the agent's tool wins,
    # as it does in the advertised schema.
    knowledge = knowledge_tool(
        knowledge_base_attachments,
        call_context.session_factory,
        credentials.api_key("openai"),
        call_context,
    )
    if knowledge is not None:
        llm.register_function(knowledge.definition.name, knowledge.handler)

    # The context starts empty — the system prompt is on the LLM service as
    # system_instruction. BYOM providers advertise no API-level tools, theirs
    # being described in the prompt instead.
    kwargs: dict[str, Any] = {"messages": []}
    if not byom_provider:
        tools_schema = _build_tools_schema(
            config, extra_tools=_with_knowledge(mcp_tools, knowledge)
        )
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
    user_params_kwargs["user_mute_strategies"] = _user_mute_strategies(config)

    context_aggregator = LLMContextAggregatorPair(
        context=context,
        user_params=LLMUserAggregatorParams(**user_params_kwargs),
    )
    if config.llm.warm_up:
        from turncall.orchestrator.llm_warm_up import warm_up_on_first_turn

        warm_up_on_first_turn(context_aggregator.user(), llm)

    # Observability: transcript taps placed early to capture before aggregation
    from turncall.orchestrator.observability import (
        AssistantTranscriptTapProcessor,
        TranscriptTapProcessor,
    )

    customer_tap = TranscriptTapProcessor(call_context=call_context)
    assistant_tap = AssistantTranscriptTapProcessor(
        call_context=call_context, llm_service=llm, tts_service=tts
    )
    observability = ObservabilityProcessor(call_context=call_context)

    # Voicemail detection (for outbound calls)
    voicemail_detector = None
    if config.voicemail_detection.enabled:
        from pipecat.extensions.voicemail.voicemail_detector import VoicemailDetector

        vm_config = config.voicemail_detection
        backoff = vm_config.backoff_plan

        # A separate lightweight LLM answers the classification. Deterministic
        # temperature, reasoning forced off — a yes/no question must not
        # inherit the agent's conversational sampling (same pattern as
        # call-analysis).
        #
        # It is the LLM *behind* a classifier, not a classifier itself. Since
        # pipecat 1.12 `Classifier` is a type in its own right (an object that
        # answers typed questions about some state), so the word belongs to it
        # and this stays named for what it is.
        voicemail_llm = _create_llm_service(
            config,
            credentials,
            temperature=0.1,
            reasoning_effort=None,
        )
        # 1.12 rewrote the detector: `llm=`/`custom_system_prompt=` are
        # deprecated and it now takes a classifier. Built explicitly rather
        # than left to the shim, which is removed in 2.0.0.
        #
        # `instructions` REPLACES the classifier's own, so an agent's custom
        # prompt goes in front of the default rather than instead of it — the
        # default is what asks for the JSON object the classifier parses, and
        # dropping it makes every verdict unreadable. Same order the shim uses.
        from pipecat.classifiers.llm.classifier import (
            DEFAULT_INSTRUCTIONS,
            LLMClassifier,
        )

        instructions = None
        if vm_config.custom_system_prompt:
            instructions = f"{vm_config.custom_system_prompt}\n\n{DEFAULT_INSTRUCTIONS}"
        voicemail_detector = VoicemailDetector(
            classifier=LLMClassifier(llm=voicemail_llm, instructions=instructions),
            voicemail_response_delay=backoff.start_at_seconds,
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
                    from turncall.events.emit import record_call_event

                    await record_call_event(
                        session,
                        call_id=call_context.call_id,
                        project_id=call_context.project_id,
                        event_type="voicemail.detected",
                        payload={"retries": _vm_retry_count},
                        # Unnumbered, as it has always been: it sorts first on
                        # the call's log. Kept as found (#312, no behaviour change).
                        sequence_number=0,
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
                openai_api_key=credentials.api_key("openai"),
                top_k=first_auto.get("top_k", 5),
                similarity_threshold=first_auto.get("similarity_threshold", 0.3),
            )
            logger.info("KB auto-retrieval enabled for {n} KBs", n=len(auto_kb_ids))

    # Video avatar: consumes TTS audio, emits avatar video. Sits right before
    # transport.output(). WebRTC + cascade only — gated by the avatar_enabled
    # flag from the caller.
    avatar = None
    if avatar_enabled and config.avatar.enabled:
        avatar = _create_avatar_service(config.avatar, credentials)

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
            *_eval_rtvi(call_context),
            stt,
            customer_tap,
            voicemail_detector.detector(),
            context_aggregator.user(),
            *([kb_processor] if kb_processor else []),
            llm,
            *_hangup_gate(call_context),
            assistant_tap,
            tts,
            voicemail_detector.gate(),
            *([avatar] if avatar else []),
            *_speech_before_ambience(config, call_context),
            transport.output(),
            # After the output: audio arrives here as it is played (see
            # test_recorder_placement). Before it, TTS bursts stretched the timeline.
            recorder,
            context_aggregator.assistant(),
            observability,
        ]
    else:
        processors = [
            transport.input(),
            *_eval_rtvi(call_context),
            stt,
            customer_tap,
            context_aggregator.user(),
            *([kb_processor] if kb_processor else []),
            llm,
            *_hangup_gate(call_context),
            assistant_tap,
            tts,
            *([avatar] if avatar else []),
            *_speech_before_ambience(config, call_context),
            transport.output(),
            # After the output: audio arrives here as it is played (see
            # test_recorder_placement). Before it, TTS bursts stretched the timeline.
            recorder,
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
        stt_model=_stt_model(
            config.stt.provider, config.stt.model, config.stt.language
        ),
        llm_info=llm_info,
        tts=config.tts.provider,
        tts_voice=config.tts.voice,
    )

    return Pipeline(processors)


def _create_client_delegate(
    config: AgentConfig,
    credentials: Credentials,
    delegation: Any,
    tools: list[Any],
    call_context: CallContext,
    *,
    knowledge: KnowledgeTool | None = None,
    hangup_gate: Any = None,
    byom_settings: BYOMSettings | None = None,
) -> Any:
    """The agent's own `llm` as gpt-live-1's delegate (#244).

    Built as cascade builds its LLM — keys through Credentials, the BYOM
    prompt-described tools — and run in pipecat's backend worker with its own
    context carrying the tools and TurnCall's handlers registered on it, so
    mocks, recording and precedence are those of every other provider.
    `delegation.instructions` replaces the system prompt, as in responses mode.
    `tools` includes `query_knowledge` when the agent has a tool-mode KB, whose
    own handler replaces the bridge's one (#254).

    The worker is outside the call's pipeline, so an ending tool reaches the
    call's `hangup_gate` directly rather than pushing from its LLM (#245) —
    to mark the call as ending; the live service arms it on relaying (#262).
    """
    from pipecat.workers.llm.backend_llm_worker import BackendLLMWorker

    from turncall.orchestrator.tool_bridge import register_tools

    update: dict[str, Any] = {"tools": tools}
    if delegation.instructions:
        update["system_prompt"] = delegation.instructions
    delegate_config = config.model_copy(update=update)
    byom_provider = config.llm.provider in ("ollama", "custom_openai")
    # A failover group when the agent has backups (#246), as cascade builds it.
    llm = _create_llm_stage(
        delegate_config,
        credentials,
        byom_settings=byom_settings,
        system_instruction=_build_system_instruction(
            delegate_config, inject_tools_prompt=byom_provider
        ),
    )
    register_tools(llm, tools, call_context, hangup_gate=hangup_gate)
    if knowledge is not None and knowledge.definition in tools:
        # Over the bridge's handler for the name, which would POST it nowhere.
        # Not in `tools` means an agent tool kept the name, and its handler.
        llm.register_function(knowledge.definition.name, knowledge.handler)
    tools_schema = None if byom_provider else _build_tools_schema(delegate_config)
    context = LLMContext(
        messages=[], **({"tools": tools_schema} if tools_schema else {})
    )
    return BackendLLMWorker(llm=llm, context=context)


def _create_s2s_pipeline(
    config: AgentConfig,
    transport: Any,
    call_context: CallContext,
    credentials: Credentials,
    pipecat_settings: PipecatSettings,
    *,
    audio_sample_rate: int = 8000,
    byom_settings: BYOMSettings | None = None,
    knowledge_base_attachments: list[dict[str, Any]] | None = None,
    knowledge_preamble: str = "",
    mcp_tools: list[Any] | None = None,
) -> Pipeline:
    """Build a speech-to-speech pipeline using OpenAI Realtime or Gemini Live.

    The S2S model handles STT + LLM + TTS in a single WebSocket connection.
    Pipeline is much simpler than cascade:
      transport.input → [VAD] → S2S_LLM → transport.output → context_agg → observability

    Knowledge bases (#254): the preamble leads the instructions, and
    `query_knowledge` is one more tool. `auto` mode has no per-turn point ahead
    of a realtime model, so pipeline_builder drops it before this, and says so.
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

    if not config.greeting_interruptible:
        logger.warning(
            "greeting_interruptible=false is not applied on S2S: the realtime "
            "service owns turn-taking."
        )

    if config.tts.pronunciations:
        # There is no TTS stage to attach the transform to — the realtime model
        # speaks for itself. Warned about for the same reason as barge-in above:
        # a setting that was accepted, stored and read must not evaporate in
        # silence.
        logger.warning(
            "tts.pronunciations is ignored on S2S (pipeline_mode='s2s'): the "
            "realtime model does its own synthesis, so there is no TTS stage "
            "to apply the {count} word(s) to.",
            count=len(config.tts.pronunciations),
        )

    from turncall.orchestrator.s2s_config import (
        create_s2s_service,
        s2s_opening_messages,
    )

    # A gateway base_url is an attacker-influenceable outbound target — gate it
    # through the same BYOM allowlist as custom text-LLM endpoints.
    if config.s2s.base_url and byom_settings:
        _validate_byom_url(config.s2s.base_url, byom_settings)

    if knowledge_preamble:
        # Ahead of the agent's own words, as cascade's _build_system_instruction
        # puts it. Every provider and delegate builds from system_prompt.
        config = config.model_copy(
            update={
                "system_prompt": "\n\n".join(
                    p for p in (knowledge_preamble, config.system_prompt) if p
                )
            }
        )

    knowledge = knowledge_tool(
        knowledge_base_attachments,
        call_context.session_factory,
        credentials.api_key("openai"),
        call_context,
    )
    extra_tools = _with_knowledge(mcp_tools, knowledge)
    advertised = _advertised_tools(config, extra_tools)
    delegation = resolve_delegation(config.s2s, has_tools=bool(advertised))
    client_mode = delegation is not None and delegation.mode == "client"
    hangup_gate = _hangup_gate(call_context)
    gate = hangup_gate[0] if hangup_gate else None
    delegate = None
    if client_mode:
        delegate = _create_client_delegate(
            config,
            credentials,
            delegation,
            advertised,
            call_context,
            knowledge=knowledge,
            hangup_gate=gate,
            byom_settings=byom_settings,
        )
    s2s_llm = create_s2s_service(
        config, credentials, tools=advertised, delegate=delegate, hangup_gate=gate
    )
    if knowledge is not None and not client_mode:
        # Before pipeline_builder registers the agent's tools, so on a name
        # clash theirs wins, as in the advertised schema.
        s2s_llm.register_function(knowledge.definition.name, knowledge.handler)
    from turncall.orchestrator.delegation_events import record_delegations

    record_delegations(s2s_llm, call_context)

    # Build context — system prompt + first_message are handled by the S2S
    # service directly (SessionProperties.instructions for OpenAI,
    # system_instruction for Gemini), plus gpt-live-1's opening (#248). Only
    # pass tools via context — except to a client delegation, whose tools live
    # on the delegate (#244).
    tools_schema = (
        None if client_mode else _build_tools_schema(config, extra_tools=extra_tools)
    )
    context_kwargs: dict[str, Any] = {"messages": s2s_opening_messages(config)}
    if tools_schema is not None:
        context_kwargs["tools"] = tools_schema
    context = LLMContext(**context_kwargs)

    # Pipecat 1.0: VAD lives on the user aggregator (needed for pipecat_vad turn detection)
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregatorParams,
    )

    from turncall.orchestrator.turn_detection import (
        s2s_vad_turn_strategies,
        share_pipecats_default_model,
    )

    # On server VAD the aggregator falls back to pipecat's default stop
    # strategy — Smart Turn — so make that the process's shared model (#271).
    share_pipecats_default_model()

    # Built unconditionally, where it used to be built only for pipecat_vad:
    # the idle guard is carried by these params, and `server_vad` is the
    # default, so leaving them None there would have quietly disabled it on
    # most S2S agents. Passing None only meant `LLMUserAggregatorParams()`, so
    # supplying one with no analyzer is the same thing plus the timeout.
    s2s_user_params = LLMUserAggregatorParams(
        user_idle_timeout=config.user_idle_timeout_ms / 1000,
        **(
            {
                "vad_analyzer": _build_vad_analyzer(
                    config, sample_rate=audio_sample_rate
                ),
                "user_turn_strategies": s2s_vad_turn_strategies(),
            }
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
        *_eval_rtvi(call_context),
        context_aggregator.user(),
        *([input_resampler] if input_resampler else []),
        customer_tap,
        s2s_llm,
        *hangup_gate,
        assistant_tap,
        *([output_resampler] if output_resampler else []),
        *_speech_before_ambience(config, call_context),
        transport.output(),
        # After the output: audio arrives here as it is played (see
        # test_recorder_placement). Before it, TTS bursts stretched the timeline.
        recorder,
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
