"""Immutable domain models (Pydantic)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from turncall.domain.enums import (
    AgentState,
    CallDirection,
    CallEventType,
    CallStatus,
    ChatChannel,
    DocumentStatus,
    KnowledgeRetrievalMode,
    ProjectRole,
    RecordingStatus,
    RoutingTargetType,
    SmsMessageRole,
    SmsSessionStatus,
    ToolExecutionMode,
    ToolExecutionStatus,
)


class DomainModel(BaseModel):
    """Base for all domain models. Frozen = immutable."""

    model_config = ConfigDict(frozen=True, from_attributes=True)


# --- Project & Auth ---


class Project(DomainModel):
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class ApiKey(DomainModel):
    id: UUID
    project_id: UUID
    key_prefix: str
    key_hash: str
    name: str
    role: ProjectRole
    environment: str | None = None
    created_at: datetime
    revoked_at: datetime | None = None


# --- Provider Config ---


class STTConfig(DomainModel):
    provider: str = "deepgram"
    model: str = "nova-3-general"
    language: str | None = "en"
    extra: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(DomainModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 1024
    base_url: str | None = None
    api_key: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    # OpenAI reasoning effort (minimal|low|medium|high). None = provider default.
    # OpenAI-family only (openai/openrouter/custom_openai); sent only when set.
    reasoning_effort: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TTSConfig(DomainModel):
    provider: str = "deepgram"
    model: str = "aura-2-helena-en"
    voice: str = "aura-2-helena-en"
    speed: float = 1.0
    extra: dict[str, Any] = Field(default_factory=dict)


# --- Tool Definition ---


class ToolDefinition(DomainModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]
    execution_mode: ToolExecutionMode = ToolExecutionMode.SYNC
    webhook_url: str | None = None
    webhook_secret: str | None = None
    timeout_seconds: int = 10
    max_retries: int = 1
    is_builtin: bool = False


# --- Agent Config ---


class ServerUrlConfig(DomainModel):
    """Server URL configuration for server events."""

    url: str | None = None
    secret: str | None = None
    timeout_seconds: float = 5.0
    events: list[str] = Field(default_factory=lambda: ["*"])


class VoicemailBackoffPlan(DomainModel):
    """Retry backoff plan for voicemail detection."""

    max_retries: int = 3
    start_at_seconds: float = 5.0
    frequency_seconds: float = 3.0


class VoicemailConfig(DomainModel):
    """Voicemail detection configuration for outbound calls."""

    enabled: bool = False
    voicemail_message: str | None = None
    backoff_plan: VoicemailBackoffPlan = Field(default_factory=VoicemailBackoffPlan)
    beep_max_await_seconds: float = 5.0
    voicemail_expected_duration_seconds: float = 15.0
    custom_system_prompt: str | None = None


class S2SConfig(DomainModel):
    """Speech-to-speech configuration for native audio-in/audio-out models."""

    provider: str = "openai"
    model: str = "gpt-realtime-2.1"
    voice: str = "alloy"
    turn_detection: str = "server_vad"  # "server_vad" | "pipecat_vad"
    # Optional OpenAI-Realtime-compatible WebSocket endpoint (wss://). Lets the
    # "openai" provider target a gateway (Vercel AI Gateway, LiteLLM) or xAI
    # direct, so models like "xai/grok-voice-think-fast-1.0" work over the same
    # protocol. None → first-party OpenAI. SSRF-gated by the BYOM allowlist.
    base_url: str | None = None
    # None = provider default. temperature is google-only (OpenAI Realtime GA
    # dropped it); max_tokens maps to max_output_tokens on openai.
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AvatarConfig(DomainModel):
    """Video avatar config (WebRTC + cascade only). HeyGen or Tavus."""

    enabled: bool = False
    provider: str = "heygen"  # "heygen" | "tavus"
    # HeyGen (LiveAvatar)
    avatar_id: str | None = None
    is_sandbox: bool = True
    # Tavus
    replica_id: str | None = None
    persona_id: str = "pipecat-stream"  # lip-syncs Pipecat TTS audio


class SuccessEvaluationConfig(DomainModel):
    """How to evaluate call success."""

    enabled: bool = False
    rubric: str = ""
    scale: str = "pass_fail"  # "pass_fail" | "likert" | "numeric"


class AnalysisConfig(DomainModel):
    """Post-call analysis configuration."""

    enabled: bool = True
    summary_enabled: bool = True
    summary_prompt: str | None = None
    success_evaluation: SuccessEvaluationConfig = Field(
        default_factory=SuccessEvaluationConfig
    )
    sentiment_enabled: bool = False
    structured_extraction_schema: dict[str, Any] | None = None
    scoring_rubric: dict[str, Any] | None = None
    # Reusable structured outputs (Takeaways, ADR-0013) attached by id.
    takeaway_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MCPServerConfig(DomainModel):
    """MCP server connection configuration."""

    name: str
    transport: str = "http"  # "http" | "sse" | "stdio"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 10
    tool_filter: list[str] | None = None


class AgentConfig(DomainModel):
    pipeline_mode: str = "cascade"  # "cascade" | "s2s"
    system_prompt: str = ""
    first_message: str | None = None
    stt: STTConfig = Field(default_factory=STTConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    language: str = "en"
    tools: list[ToolDefinition] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    silence_timeout_ms: int = 800
    interruption_enabled: bool = True
    smart_turn_detection: bool = True  # Use ML-based turn detection (LocalSmartTurnV3)
    smart_turn_stop_secs: float = 1.0  # Max silence before forcing end-of-turn
    max_call_duration_seconds: int = 3600
    voicemail_detection: VoicemailConfig = Field(
        default_factory=lambda: VoicemailConfig()
    )
    s2s: S2SConfig = Field(default_factory=S2SConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    transport: str = "twilio"  # "twilio" | "webrtc" | "both"
    server_url: ServerUrlConfig = Field(default_factory=ServerUrlConfig)
    knowledge_bases: list["KnowledgeBaseAttachment"] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseAttachment(DomainModel):
    """Links a knowledge base to an agent with retrieval settings."""

    knowledge_base_id: str
    mode: KnowledgeRetrievalMode = KnowledgeRetrievalMode.AUTO
    priority: int = 0
    top_k: int = 5
    similarity_threshold: float = 0.3
    tool_description: str | None = None  # LLM instruction (tool mode only)


class Agent(DomainModel):
    id: UUID
    project_id: UUID
    name: str
    environment: str
    version: int
    state: AgentState
    config: AgentConfig
    created_at: datetime
    published_at: datetime | None = None


# --- Phone Number ---


class PhoneNumber(DomainModel):
    id: UUID
    project_id: UUID
    provider: str = "twilio"
    external_number_sid: str
    e164_number: str
    routing_target_type: RoutingTargetType
    routing_target_id: UUID | None = None
    server_url: str | None = None
    sms_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Call ---


class Call(DomainModel):
    id: UUID
    project_id: UUID
    provider: str = "twilio"
    provider_call_sid: str | None = None
    parent_provider_call_sid: str | None = None
    direction: CallDirection
    from_number: str | None = None
    to_number: str | None = None
    active_agent_id: UUID | None = None
    workflow_id: UUID | None = None
    status: CallStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    recording_status: RecordingStatus = RecordingStatus.NONE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Tool Invocation ---


class ToolInvocation(DomainModel):
    id: UUID
    call_id: UUID
    tool_name: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None = None
    status: ToolExecutionStatus
    latency_ms: int | None = None
    idempotency_key: str | None = None
    created_at: datetime


# --- Call Event ---


class CallEvent(DomainModel):
    id: UUID
    call_id: UUID
    event_type: CallEventType
    provider_timestamp: datetime | None = None
    internal_timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence_number: int = 0


# --- SMS / Chat ---


class SmsSession(DomainModel):
    id: UUID
    project_id: UUID
    agent_id: UUID
    phone_number_id: UUID | None = None
    customer_number: str
    turncall_number: str
    status: SmsSessionStatus
    channel: ChatChannel
    message_count: int = 0
    last_activity_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SmsMessage(DomainModel):
    id: UUID
    session_id: UUID
    project_id: UUID
    role: SmsMessageRole
    content: str
    channel: ChatChannel
    provider_message_sid: str | None = None
    token_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Knowledge Base ---


class KnowledgeBase(DomainModel):
    id: UUID
    project_id: UUID
    name: str
    description: str | None = None
    embedding_model: str = "text-embedding-3-small"
    chunk_size: int = 512
    chunk_overlap: int = 64
    created_at: datetime
    updated_at: datetime


class Document(DomainModel):
    id: UUID
    knowledge_base_id: UUID
    filename: str
    content_type: str
    storage_key: str
    char_count: int
    chunk_count: int
    status: DocumentStatus
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentChunk(DomainModel):
    id: UUID
    document_id: UUID
    knowledge_base_id: UUID
    chunk_index: int
    content: str
    token_count: int
    created_at: datetime
