"""Agent API schemas with provider config validation."""

from datetime import datetime
from typing import Any
from uuid import UUID

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_serializer,
    model_validator,
)

from turncall.domain.config_secrets import sanitize_config as _sanitize_config
from turncall.domain.ipa import normalize_pronunciations
from turncall.domain.models import BUILTIN_TOOL_NAMES
from turncall.domain.providers import (
    AVATAR_PROVIDERS,
    LLM_PROVIDERS,
    S2S_PROVIDERS,
    STT_PROVIDERS,
    TTS_PROVIDERS,
)


class STTConfigSchema(BaseModel):
    provider: str = "deepgram"
    # Empty = the chosen provider's own default. Not `nova-3-general`: that is
    # Deepgram's model, and stamping it into every agent's config_blob sent it
    # to OpenAI, ElevenLabs and Cartesia too, each of which 400s on it.
    model: str = ""
    language: str | None = "en"
    keyterms: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider(self) -> "STTConfigSchema":
        supported = set(STT_PROVIDERS)
        if self.provider not in supported:
            msg = f"Unsupported STT provider: {self.provider}. Supported: {supported}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def normalize_keyterms(self) -> "STTConfigSchema":
        """Drop blanks and duplicates, keeping the order given.

        A blank keyterm is a repeated query parameter with no content on the
        providers that send them as such. Deduplicating matters because
        Cartesia caps a connection at 100 terms and spends the budget in the
        order it receives them.
        """
        seen: set[str] = set()
        cleaned: list[str] = []
        for term in self.keyterms:
            stripped = term.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                cleaned.append(stripped)
        self.keyterms = cleaned
        return self


class LLMConfigSchema(BaseModel):
    provider: str = "openai"
    # Empty = the provider's house model. Not `gpt-4o-mini`: that is OpenAI's,
    # and as the default of a provider-agnostic field it reached Anthropic,
    # Bedrock, Ollama and OpenRouter too. Providers with no house model are
    # rejected below rather than guessed at.
    model: str = ""
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=128000)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=512)
    fallback_models: list[str] = Field(default_factory=list)
    # OpenAI reasoning effort — reasoning models only (o-series/gpt-5), OpenAI-family.
    reasoning_effort: str | None = Field(
        default=None, pattern="^(minimal|low|medium|high)$"
    )
    extra: dict[str, Any] = Field(default_factory=dict)
    # Backup LLMs for a voice call (#222), each a whole LLM config of its own.
    failover: list["LLMConfigSchema"] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_failover_flat(self) -> "LLMConfigSchema":
        if any(entry.failover for entry in self.failover):
            msg = "a failover entry cannot nest its own failover list"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_provider(self) -> "LLMConfigSchema":
        supported = set(LLM_PROVIDERS)
        if self.provider not in supported:
            msg = f"Unsupported LLM provider: {self.provider}. Supported: {supported}"
            raise ValueError(msg)
        if self.provider == "bedrock" and self.base_url:
            # Rejected rather than ignored: Bedrock is reached through boto3,
            # not a URL, so a base_url here would silently do nothing.
            msg = (
                "base_url is not supported for the bedrock provider "
                "(see adr/0016). Set config.aws.region instead."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_byom_fields(self) -> "LLMConfigSchema":
        if self.provider == "custom_openai" and not self.base_url:
            msg = "base_url is required for custom_openai provider"
            raise ValueError(msg)
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            msg = "base_url must start with http:// or https://"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_model_present_where_required(self) -> "LLMConfigSchema":
        """Reject an unnamed model where there is nothing sensible to default.

        A Bedrock id's availability is region-specific (adr/0016), an Ollama
        model is whatever is pulled onto that host, a BYOM endpoint decides
        for itself, and OpenRouter exists to route between vendors. Caught at
        create time rather than at the first call, where it would be a dead
        conversation instead of a 422.
        """
        from turncall.services.llm_models import model_is_required

        if model_is_required(self.provider) and not self.model:
            msg = f"model is required for provider '{self.provider}'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_fallback_models(self) -> "LLMConfigSchema":
        if self.fallback_models and self.provider != "openrouter":
            msg = "fallback_models requires provider 'openrouter'"
            raise ValueError(msg)
        return self


class TTSConfigSchema(BaseModel):
    provider: str = "deepgram"
    model: str = "aura-2-helena-en"
    voice: str = "aura-2-helena-en"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    # Word -> IPA. The outbound mirror of `stt.keyterms`: one per-agent
    # vocabulary hint, four provider dialects, cleaned up at this same
    # boundary. One IPA spelling works everywhere because each provider's
    # markup is generated from it in the pipeline factory.
    pronunciations: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def clean_pronunciations(self) -> "TTSConfigSchema":
        """Drop blanks, canonicalize the notation, reject what is not IPA.

        Pipecat never rejects — it would tokenize `met-FOR-min` happily, insert
        it into the markup and change nothing audible. See domain/ipa.py.
        """
        self.pronunciations = normalize_pronunciations(self.pronunciations)
        return self

    @model_validator(mode="after")
    def validate_provider(self) -> "TTSConfigSchema":
        supported = set(TTS_PROVIDERS)
        if self.provider not in supported:
            msg = f"Unsupported TTS provider: {self.provider}. Supported: {supported}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_voice(self) -> "TTSConfigSchema":
        openai_voices = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        if self.provider == "openai" and self.voice not in openai_voices:
            msg = f"Invalid OpenAI voice: {self.voice}. Options: {openai_voices}"
            raise ValueError(msg)
        return self


class ToolDefinitionSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z_][a-z0-9_]*$")
    description: str = Field(..., min_length=1, max_length=1024)
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str = Field(default="sync", pattern=r"^(sync|async)$")
    webhook_url: str | None = None
    # When set, tool webhook POSTs are HMAC-signed (X-TurnCall-Signature,
    # same v1= scheme as event webhooks) so the receiver can verify origin.
    webhook_secret: str | None = Field(default=None, min_length=16, max_length=256)
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    max_retries: int = Field(default=1, ge=0, le=5)

    @model_validator(mode="after")
    def validate_webhook_required_for_non_builtin(self) -> "ToolDefinitionSchema":
        if self.name not in BUILTIN_TOOL_NAMES and not self.webhook_url:
            msg = f"Non-built-in tool '{self.name}' requires a webhook_url"
            raise ValueError(msg)
        return self


class GuardrailsSchema(BaseModel):
    max_tool_calls_per_turn: int = Field(default=5, ge=1, le=20)
    prohibited_topics: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class SuccessEvaluationSchema(BaseModel):
    """Defines how to evaluate call success."""

    enabled: bool = False
    rubric: str = Field(
        default="",
        max_length=4000,
        description="Criteria for the LLM to judge success",
    )
    scale: str = Field(
        default="pass_fail",
        pattern=r"^(pass_fail|likert|numeric)$",
        description="pass_fail | likert (1-5) | numeric (0-100)",
    )


class AnalysisSchema(BaseModel):
    """Post-call analysis configuration. Results ship inline in `call.ended`."""

    enabled: bool = True
    summary_enabled: bool = True
    summary_prompt: str | None = Field(
        default=None,
        max_length=4000,
        description="Custom prompt for summary generation",
    )
    success_evaluation: SuccessEvaluationSchema = Field(
        default_factory=SuccessEvaluationSchema
    )
    sentiment_enabled: bool = False
    structured_extraction_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for structured data extraction from transcript",
    )
    scoring_rubric: dict[str, Any] | None = Field(
        default=None,
        description="Custom scoring rubric with named criteria",
    )
    # Strings, not UUID type: config is stored via model_dump() into JSONB,
    # which can't serialize UUID objects. Validated below.
    takeaway_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Takeaways (reusable structured outputs) to extract after "
        "each call; results keyed by takeaway name in analysis.takeaways",
    )

    @model_validator(mode="after")
    def validate_takeaway_ids(self) -> "AnalysisSchema":
        for t in self.takeaway_ids:
            try:
                UUID(t)
            except ValueError as exc:
                msg = f"takeaway_ids entries must be UUIDs, got {t!r}"
                raise ValueError(msg) from exc
        return self

    model: str | None = Field(
        default=None,
        max_length=100,
        description="LLM model override for analysis (default: agent's LLM model)",
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class ServerUrlConfigSchema(BaseModel):
    """Server URL for server events."""

    url: str | None = Field(default=None, max_length=2048)
    secret: str | None = Field(default=None, max_length=128)
    timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    events: list[str] = Field(default_factory=lambda: ["*"])


class VoicemailBackoffSchema(BaseModel):
    max_retries: int = Field(default=3, ge=0, le=10)
    start_at_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    frequency_seconds: float = Field(default=3.0, ge=1.0, le=15.0)


class VoicemailDetectionSchema(BaseModel):
    """Voicemail detection for outbound calls."""

    enabled: bool = False
    voicemail_message: str | None = Field(
        default=None, max_length=2000, description="TTS message to leave on voicemail"
    )
    backoff_plan: VoicemailBackoffSchema = Field(
        default_factory=VoicemailBackoffSchema,
        description="Retry plan: re-classify periodically until confident",
    )
    beep_max_await_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Max time to wait for beep after voicemail detected",
    )
    voicemail_expected_duration_seconds: float = Field(
        default=15.0, ge=5.0, le=60.0, description="Expected voicemail greeting length"
    )
    custom_system_prompt: str | None = Field(
        default=None, max_length=4000, description="Custom classifier prompt"
    )


class DelegationConfigSchema(BaseModel):
    """gpt-live-1's delegate (#243): the model it hands tool and reasoning
    work to. Only `responses` (an OpenAI-hosted delegate) for now."""

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="responses", pattern=r"^responses$")
    model: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning_effort: str | None = Field(
        default=None, pattern=r"^(none|minimal|low|medium|high|xhigh|max)$"
    )
    service_tier: str | None = Field(
        default=None, pattern=r"^(auto|default|flex|priority)$"
    )
    instructions: str | None = Field(default=None, min_length=1, max_length=128000)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)

    @model_validator(mode="after")
    def validate_model_present(self) -> "DelegationConfigSchema":
        if self.mode == "responses" and not self.model:
            msg = (
                "s2s.delegation in responses mode needs a model: it names the "
                "OpenAI-hosted model gpt-live-1 delegates to"
            )
            raise ValueError(msg)
        return self


class S2SConfigSchema(BaseModel):
    """Speech-to-speech model configuration."""

    provider: str = "openai"
    model: str = "gpt-realtime-2.1"
    voice: str = "alloy"
    turn_detection: str = Field(
        default="server_vad", pattern=r"^(server_vad|pipecat_vad)$"
    )
    # OpenAI-Realtime-compatible gateway/endpoint (wss://). openai provider only.
    base_url: str | None = Field(default=None, max_length=2048)
    # None = provider default. temperature is google-only (OpenAI Realtime GA
    # has no temperature control); max_tokens maps to max_output_tokens on openai.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=128000)
    extra: dict[str, Any] = Field(default_factory=dict)
    delegation: DelegationConfigSchema | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> "S2SConfigSchema":
        supported = set(S2S_PROVIDERS)
        if self.provider not in supported:
            msg = f"Unsupported S2S provider: {self.provider}. Supported: {supported}"
            raise ValueError(msg)
        if self.provider == "openai_live" and self.turn_detection == "pipecat_vad":
            msg = (
                "gpt-live-1 is full duplex and handles interruption itself, so "
                "client-side turn detection would fight it; remove "
                "s2s.turn_detection or leave it at 'server_vad'"
            )
            raise ValueError(msg)
        if self.delegation is not None and self.provider != "openai_live":
            msg = (
                f"s2s.delegation is not supported for provider '{self.provider}': "
                "only openai_live delegates — the other S2S providers call "
                "tools themselves"
            )
            raise ValueError(msg)
        if self.provider == "aws" and self.base_url:
            msg = (
                "base_url is not supported for the aws S2S provider "
                "(see adr/0016). Set config.aws.region instead."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_temperature_support(self) -> "S2SConfigSchema":
        if self.provider == "openai" and self.temperature is not None:
            msg = (
                "The OpenAI Realtime GA API does not support temperature; "
                "remove s2s.temperature or use provider 'google'"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_base_url(self) -> "S2SConfigSchema":
        if self.base_url is None:
            return self
        # Realtime is a WebSocket protocol; the gateway URL must be ws(s)://.
        if not self.base_url.startswith(("ws://", "wss://")):
            msg = "S2S base_url must start with ws:// or wss://"
            raise ValueError(msg)
        # Only the openai (OpenAI-Realtime) provider speaks this endpoint;
        # Gemini Live uses its own SDK transport.
        if self.provider != "openai":
            msg = "S2S base_url is only supported for the openai provider"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_voice(self) -> "S2SConfigSchema":
        openai_voices = {
            "alloy",
            "ash",
            "ballad",
            "coral",
            "echo",
            "sage",
            "shimmer",
            "verse",
        }
        # A gateway base_url routes to non-OpenAI models (e.g. Grok) with their
        # own voice sets, which we can't enumerate — let the gateway validate.
        if (
            self.provider == "openai"
            and self.base_url is None
            and self.voice not in openai_voices
        ):
            msg = (
                f"Invalid OpenAI Realtime voice: {self.voice}. Options: {openai_voices}"
            )
            raise ValueError(msg)
        # Gemini Live's native-audio voice set grows with each model (30+ and
        # counting), so we don't allowlist it — Gemini validates on connect.
        return self


class MCPServerSchema(BaseModel):
    """MCP server connection configuration."""

    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z_][a-z0-9_-]*$")
    transport: str = Field(default="http", pattern=r"^(http|sse|stdio)$")
    url: str | None = Field(
        default=None,
        max_length=2048,
        description="Server URL (required for http/sse transport)",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Custom headers sent with requests (e.g. Authorization)",
    )
    command: str | None = Field(
        default=None,
        max_length=256,
        description="Executable command (required for stdio transport)",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Command arguments (stdio transport)",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for subprocess (stdio transport)",
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Connection timeout",
    )
    tool_filter: list[str] | None = Field(
        default=None,
        description="Only expose these tools (None = all tools)",
    )

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerSchema":
        if self.transport in ("http", "sse") and not self.url:
            msg = f"url is required for {self.transport} transport"
            raise ValueError(msg)
        if self.transport == "stdio" and not self.command:
            msg = "command is required for stdio transport"
            raise ValueError(msg)
        if self.url and not self.url.startswith(("http://", "https://")):
            msg = "url must start with http:// or https://"
            raise ValueError(msg)
        return self


class AvatarConfigSchema(BaseModel):
    """Video avatar config (WebRTC + cascade only). HeyGen or Tavus."""

    enabled: bool = False
    provider: str = Field(
        default="heygen", pattern=rf"^({'|'.join(AVATAR_PROVIDERS)})$"
    )
    # HeyGen (LiveAvatar)
    avatar_id: str | None = Field(default=None, max_length=256)
    is_sandbox: bool = True
    # Tavus
    replica_id: str | None = Field(default=None, max_length=256)
    persona_id: str = Field(default="pipecat-stream", max_length=256)


class AmbienceConfigSchema(BaseModel):
    """A room the agent sounds like it is in (ADR-0021). Absent means off.

    No `enabled` flag: the sound id's absence already says off, and two ways
    to say it are two things that can disagree.
    """

    model_config = ConfigDict(extra="forbid")

    sound: UUID = Field(description="An ambience sound in this project")
    # 0.3, not pipecat's 0.4: an 8 kHz companded line carries background energy
    # more intrusively than a 16 kHz browser call. No ceiling below 1.0: the
    # PSTN echo check (#171) found no spurious interruptions at 0.3.
    volume: float = Field(default=0.3, ge=0.0, le=1.0)

    @field_serializer("sound")
    def _sound_as_str(self, sound: UUID) -> str:
        # config_blob is JSONB and model_dump() is python-mode.
        return str(sound)


class AWSCredentialSetSchema(BaseModel):
    """The shape of an AWS credential set — one schema for both places it lives.

    An agent's `aws` block (below) and an `aws` project credential (ADR-0020)
    validate the same fields the same way; only the agent block is gated by
    AWS_AGENT_CREDENTIALS_ENABLED.
    """

    region: str | None = Field(default=None, max_length=64)
    access_key_id: str | None = Field(default=None, max_length=128)
    secret_access_key: str | None = Field(default=None, max_length=512)
    session_token: str | None = Field(default=None, max_length=4096)
    role_arn: str | None = Field(default=None, max_length=2048)
    external_id: str | None = Field(default=None, max_length=1224)
    profile: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_key_pair(self) -> "AWSCredentialSetSchema":
        if bool(self.access_key_id) != bool(self.secret_access_key):
            msg = (
                "aws.access_key_id and aws.secret_access_key must be set "
                "together, or neither"
            )
            raise ValueError(msg)
        return self


class AWSConfigSchema(AWSCredentialSetSchema):
    """AWS credentials/region shared by the bedrock LLM and aws S2S providers.

    One block per agent so both providers authenticate as the same principal.
    See adr/0016.
    """

    @model_validator(mode="after")
    def validate_credentials(self) -> "AWSConfigSchema":
        if self.access_key_id:
            # Rejected here, at create, rather than at call time — an agent that
            # looks healthy in the API and fails with a customer on the line is
            # the worse failure. See adr/0016. Agent config only: a project
            # credential is encrypted and outside this surface (ADR-0020).
            from turncall.config.settings import get_settings

            if not get_settings().aws.agent_credentials_enabled:
                msg = (
                    "Per-agent static AWS keys are disabled. Set "
                    "AWS_AGENT_CREDENTIALS_ENABLED=true to allow them, use "
                    "aws.role_arn, which stores no durable secret, or store "
                    "the keys as the project's aws credential "
                    "(PUT /v1/credentials/aws), which is encrypted."
                )
                raise ValueError(msg)
        return self


class AgentConfigSchema(BaseModel):
    # Reject unknown fields instead of silently dropping them — a typo'd or
    # schema-lagging config section (e.g. a mis-nested "avatar") used to vanish
    # at ingest with no error. Every domain-settable field is declared below;
    # knowledge_bases is intentionally attached via /agents/{id}/knowledge-bases,
    # not the config, so it must not appear here.
    model_config = ConfigDict(extra="forbid")

    pipeline_mode: str = Field(default="cascade", pattern=r"^(cascade|s2s)$")
    system_prompt: str = Field(default="", max_length=128000)
    first_message: str | None = Field(default=None, max_length=2000)
    stt: STTConfigSchema = Field(default_factory=STTConfigSchema)
    llm: LLMConfigSchema = Field(default_factory=LLMConfigSchema)
    tts: TTSConfigSchema = Field(default_factory=TTSConfigSchema)
    language: str = Field(default="en", max_length=10)
    tools: list[ToolDefinitionSchema] = Field(default_factory=list)
    mcp_servers: list[MCPServerSchema] = Field(default_factory=list)
    guardrails: GuardrailsSchema = Field(default_factory=GuardrailsSchema)
    analysis: AnalysisSchema = Field(default_factory=AnalysisSchema)
    silence_timeout_ms: int = Field(default=800, ge=200, le=5000)
    user_idle_timeout_ms: int = Field(
        default=10000,
        ge=0,
        le=300000,
        description=(
            "Silence after the agent stops speaking before the idle guard "
            "reacts: first it speaks idle_message, then it ends the call. "
            "0 disables. Not silence_timeout_ms, which is the VAD stop window "
            "inside a turn."
        ),
    )
    idle_message: str = Field(
        default="Are you still there?",
        max_length=500,
        description="Spoken on the first idle timeout. Cascade speaks it "
        "verbatim; S2S has no TTS stage, so the model is asked to check in.",
    )
    interruption_enabled: bool = True
    smart_turn_detection: bool = Field(
        default=True, description="Use ML-based turn detection (SmartTurnV3)"
    )
    smart_turn_stop_secs: float = Field(
        default=1.0,
        ge=0.5,
        le=10.0,
        description="Max silence before forcing end-of-turn",
    )
    max_call_duration_seconds: int = Field(default=3600, ge=60, le=14400)
    voicemail_detection: VoicemailDetectionSchema = Field(
        default_factory=lambda: VoicemailDetectionSchema()
    )
    s2s: S2SConfigSchema = Field(default_factory=S2SConfigSchema)
    aws: AWSConfigSchema = Field(default_factory=AWSConfigSchema)
    avatar: AvatarConfigSchema = Field(default_factory=AvatarConfigSchema)
    ambience: AmbienceConfigSchema | None = None
    transport: str = Field(default="twilio", pattern=r"^(twilio|webrtc|both)$")
    server_url: ServerUrlConfigSchema = Field(default_factory=ServerUrlConfigSchema)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_tool_names(self) -> "AgentConfigSchema":
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            msg = "Tool names must be unique within an agent"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_s2s_constraints(self) -> "AgentConfigSchema":
        if self.pipeline_mode == "s2s" and self.voicemail_detection.enabled:
            msg = "Voicemail detection is not supported in S2S pipeline mode"
            raise ValueError(msg)
        if self.pipeline_mode == "s2s" and self.llm.failover:
            msg = (
                "llm.failover is not supported in S2S pipeline mode: the "
                "conversation lives inside the realtime session, so no other "
                "provider could carry it on"
            )
            raise ValueError(msg)
        if (
            self.pipeline_mode == "s2s"
            and self.s2s.provider == "openai_live"
            and any(t.name == "handoff_to_agent" for t in self.tools)
        ):
            msg = (
                "handoff_to_agent is not supported on openai_live: a handoff "
                "would swap the delegate's tools but not gpt-live-1's session, "
                "leaving the previous agent's voice and prompt in place"
            )
            raise ValueError(msg)
        return self


# --- Request/Response ---


class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    environment: str = Field(
        default="development", pattern=r"^(development|staging|production)$"
    )
    config: AgentConfigSchema = Field(default_factory=AgentConfigSchema)


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: AgentConfigSchema | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    environment: str
    version: int
    state: str
    config: dict[str, Any]
    created_at: datetime
    published_at: datetime | None

    # Derived, not stored (#148), the shape #95 set for eval scenarios: two
    # things an author needs told that cannot be refusals — a "pronunciation"
    # that is the spelling typed back (legal IPA notation, so the validator
    # cannot see it) and a voice that will not read the markup (#147 said this
    # in a build-time log, mid-call, to whoever reads server logs). Computed on
    # every read, so an agent whose model changed under its pronunciations says
    # so too.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def warnings(self) -> list[dict[str, Any]]:
        from turncall.domain.models import AgentConfig
        from turncall.orchestrator.config_warnings import config_warnings

        try:
            parsed = AgentConfig.model_validate(self.config)
        except ValidationError as exc:
            # A stored config this cannot parse has a louder problem than any
            # warning, and an advisory check must never cost the caller their
            # agent read. Not silent: `coding-style.md`.
            logger.debug("agent_warning_check_skipped: {error}", error=str(exc))
            return []
        return config_warnings(parsed)

    @classmethod
    def from_row(cls, row: Any) -> "AgentResponse":
        config = row.config_blob
        config = _sanitize_config(config)
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            environment=row.environment,
            version=row.version,
            state=row.state,
            config=config,
            created_at=row.created_at,
            published_at=row.published_at,
        )
