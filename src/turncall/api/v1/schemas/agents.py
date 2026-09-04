"""Agent API schemas with provider config validation."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class STTConfigSchema(BaseModel):
    provider: str = "deepgram"
    model: str = "nova-3-general"
    language: str | None = "en"
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider(self) -> "STTConfigSchema":
        supported = {"openai", "deepgram", "elevenlabs", "cartesia"}
        if self.provider not in supported:
            msg = f"Unsupported STT provider: {self.provider}. Supported: {supported}"
            raise ValueError(msg)
        return self


class LLMConfigSchema(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
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

    @model_validator(mode="after")
    def validate_provider(self) -> "LLMConfigSchema":
        supported = {"openai", "ollama", "custom_openai", "anthropic", "openrouter"}
        if self.provider not in supported:
            msg = f"Unsupported LLM provider: {self.provider}. Supported: {supported}"
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
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider(self) -> "TTSConfigSchema":
        supported = {"openai", "deepgram", "elevenlabs", "cartesia"}
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
        builtin_tools = {
            "end_call",
            "transfer_call",
            "handoff_to_agent",
            "send_dtmf",
        }
        if self.name not in builtin_tools and not self.webhook_url:
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

    @model_validator(mode="after")
    def validate_provider(self) -> "S2SConfigSchema":
        supported = {"openai", "google"}
        if self.provider not in supported:
            msg = f"Unsupported S2S provider: {self.provider}. Supported: {supported}"
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
    provider: str = Field(default="heygen", pattern=r"^(heygen|tavus)$")
    # HeyGen (LiveAvatar)
    avatar_id: str | None = Field(default=None, max_length=256)
    is_sandbox: bool = True
    # Tavus
    replica_id: str | None = Field(default=None, max_length=256)
    persona_id: str = Field(default="pipecat-stream", max_length=256)


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
    avatar: AvatarConfigSchema = Field(default_factory=AvatarConfigSchema)
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


_MASK = "***"


def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Mask every secret in an agent config before returning it in API responses.

    A read-only key must not be able to exfiltrate credentials for external
    systems, so this covers all secret-bearing fields, not just llm.api_key:
    - llm.api_key (BYOM provider key)
    - server_url.secret (server-events signing secret)
    - tools[].webhook_secret (custom-tool signing secret)
    - mcp_servers[].headers / .env (documented as carrying Authorization)

    New secret-bearing config fields MUST be added here.
    """
    if not isinstance(config, dict):
        return config
    out = {**config}

    llm = out.get("llm")
    if isinstance(llm, dict) and llm.get("api_key") is not None:
        out["llm"] = {**llm, "api_key": _MASK}

    server_url = out.get("server_url")
    if isinstance(server_url, dict) and server_url.get("secret") is not None:
        out["server_url"] = {**server_url, "secret": _MASK}

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            {**t, "webhook_secret": _MASK}
            if isinstance(t, dict) and t.get("webhook_secret") is not None
            else t
            for t in tools
        ]

    mcp_servers = out.get("mcp_servers")
    if isinstance(mcp_servers, list):
        out["mcp_servers"] = [_mask_mcp_server(s) for s in mcp_servers]

    return out


def _mask_mcp_server(server: Any) -> Any:
    """Mask header/env values (keeping keys, so the shape stays visible)."""
    if not isinstance(server, dict):
        return server
    masked = {**server}
    if isinstance(server.get("headers"), dict) and server["headers"]:
        masked["headers"] = {k: _MASK for k in server["headers"]}
    if isinstance(server.get("env"), dict) and server["env"]:
        masked["env"] = {k: _MASK for k in server["env"]}
    return masked
