"""Immutable domain models (Pydantic)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)

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
    # Empty means "the provider's own default", resolved in the pipeline
    # factory. It used to default to `nova-3-general`, which is Deepgram's
    # model — every other provider rejects it with a 400, so an agent that
    # named no model had no working STT at all.
    model: str = ""
    language: str | None = "en"
    # Vocabulary hints — product names, SKUs, surnames. Every provider supports
    # these and every provider spells them differently (`keyterm`, `keywords`,
    # `keyterms`), and on Deepgram the wrong spelling is a 400 that kills the
    # call rather than a knob that does nothing. One field; the mapping to each
    # provider's dialect lives in the pipeline factory.
    keyterms: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    # Backup STTs, in order, a call moves to when this one stops being usable
    # (#223). Voice cascade only; each entry is a whole STT config of its own.
    failover: list["STTConfig"] = Field(default_factory=list)


class LLMConfig(DomainModel):
    provider: str = "openai"
    # Empty means "the provider's house model", resolved by
    # services/llm_models.resolve_llm_model — which raises for the providers
    # that have none. It used to default to `gpt-4o-mini`, which is OpenAI's
    # model: Anthropic answers `404 not_found_error: model: gpt-4o-mini`.
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 1024
    base_url: str | None = None
    api_key: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    # OpenAI reasoning effort (minimal|low|medium|high). None = provider default.
    # OpenAI-family only (openai/openrouter/custom_openai); sent only when set.
    reasoning_effort: str | None = None
    # Send a one-token completion as the caller's first turn starts, so the
    # first reply does not pay for a cold client (orchestrator/llm_warm_up.py).
    warm_up: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)
    # Backup LLMs, in order, a call moves to when this one stops being usable
    # (#222). Voice cascade only; each entry's key resolves like any other.
    failover: list["LLMConfig"] = Field(default_factory=list)


class TTSConfig(DomainModel):
    provider: str = "deepgram"
    model: str = "aura-2-helena-en"
    voice: str = "aura-2-helena-en"
    speed: float = 1.0
    # Word -> IPA, the outbound mirror of `STTConfig.keyterms`. Validated and
    # canonicalized at the API boundary (domain/ipa.py); the mapping to each
    # provider's own markup lives in the pipeline factory, since pipecat
    # exposes it as a classmethod on the TTS service class.
    pronunciations: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    # Backup TTSs, in order (#223), like `STTConfig.failover`.
    failover: list["TTSConfig"] = Field(default_factory=list)


# --- Tool Definition ---


# Tools TurnCall implements itself. Every one resolves through call_control
# against a live call_id, which is why the text channels can't offer them.
BUILTIN_TOOL_NAMES = frozenset(
    {
        "end_call",
        "transfer_call",
        "handoff_to_agent",
        "send_dtmf",
        "do_not_call",
        # Offered by TurnCall itself to an agent with skills (#424), never
        # listed in its `tools`; a built-in so no skill or MCP tool takes it.
        "load_skill",
    }
)


class ToolDefinition(DomainModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]
    execution_mode: ToolExecutionMode = ToolExecutionMode.SYNC
    webhook_url: str | None = None
    webhook_secret: str | None = None
    timeout_seconds: int = 10
    max_retries: int = 1


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


class DelegationConfig(DomainModel):
    """How an `openai_live` agent's delegate is set up. See CONTEXT.md,
    "Delegation". `responses` is an OpenAI-hosted delegate (#243); `client` is
    the agent's own `llm` (#244), and the default for an agent with tools."""

    mode: str = "responses"  # "client" | "responses"
    # responses mode only
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    # Replaces what the delegate is told; gpt-live-1 keeps the system_prompt.
    instructions: str | None = None
    timeout_seconds: float = 30.0


class S2SConfig(DomainModel):
    """Speech-to-speech configuration for native audio-in/audio-out models."""

    provider: str = "openai"
    model: str = "gpt-realtime-2.1"
    voice: str = "alloy"
    turn_detection: str = "server_vad"  # "server_vad" | "pipecat_vad"
    # Optional OpenAI-Realtime-compatible WebSocket endpoint (wss://). Lets the
    # "openai" provider target a gateway (Vercel AI Gateway, LiteLLM), so
    # models like "openai/gpt-realtime-2" work over the same protocol. Grok
    # Voice is the "xai" provider. None → first-party OpenAI. SSRF-gated by the
    # BYOM allowlist.
    base_url: str | None = None
    # None = provider default. temperature is google-only (OpenAI Realtime GA
    # dropped it); max_tokens maps to max_output_tokens on openai.
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    delegation: DelegationConfig | None = None  # openai_live only


def resolve_delegation(s2s: Any, *, has_tools: bool) -> Any:
    """An `openai_live` agent's effective delegation, or None.

    The block when there is one; `s2s.extra.backend_model` read as responses
    mode with that model (it predates the block, #243); client mode when the
    agent has tools (#244); otherwise none — a purely conversational agent
    pays for no delegate. Duck-typed over `s2s` so the API schema applies the
    same rule as the pipeline; returns that side's own delegation object.
    """
    if s2s.provider != "openai_live":
        return None
    if s2s.delegation is not None:
        return s2s.delegation
    backend_model = s2s.extra.get("backend_model")
    if backend_model:
        return DelegationConfig(
            model=backend_model, service_tier=s2s.extra.get("service_tier")
        )
    return DelegationConfig(mode="client") if has_tools else None


class AvatarConfig(DomainModel):
    """Video avatar config (WebRTC only, cascade or S2S). HeyGen or Tavus."""

    enabled: bool = False
    provider: str = "heygen"  # "heygen" | "tavus"
    # HeyGen (LiveAvatar)
    avatar_id: str | None = None
    is_sandbox: bool = True
    # Tavus
    replica_id: str | None = None
    persona_id: str = "pipecat-stream"  # lip-syncs Pipecat TTS audio


class AmbienceConfig(DomainModel):
    """A room the agent sounds like it is in (ADR-0021). Absent means off.

    `sound` names a project's ambience sound; it is mixed into the outbound
    audio of every voice transport, S2S included, and never into an eval.
    """

    sound: UUID
    volume: float = Field(default=0.3, ge=0.0, le=1.0)


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


class AWSConfig(DomainModel):
    """AWS credentials and region for the `bedrock` LLM and `aws` S2S providers.

    One block per agent rather than one per role: an agent uses a single AWS
    account, and duplicating credentials would let the LLM and the voice leg
    drift into authenticating as different principals — invisible through the
    API, since both are masked. See ADR-0016.

    Resolution order is role_arn > static keys > profile > the ambient boto3
    chain (env, SSO cache, instance profile, IRSA).
    """

    region: str | None = None  # None → settings.aws.region (AWS_REGION)
    # Static keys are the deliberate escape hatch, gated at the API boundary by
    # AWS_AGENT_CREDENTIALS_ENABLED: they are long-lived, and land in
    # config_blob (encrypted at rest under CREDENTIAL_ENCRYPTION_KEY, #338).
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    # Preferred multi-tenant path — assumed from platform credentials, yields
    # temporary credentials and persists no durable secret.
    role_arn: str | None = None
    external_id: str | None = None
    # Named profile for the ambient chain; how an SSO login is selected.
    profile: str | None = None


class KnowledgeBaseAttachment(DomainModel):
    """A knowledge base attached to an agent, with its retrieval settings.

    A stored agent's live on its link rows; this is the shape an inline
    agent's `knowledge_bases` names them in (#260). A call-init webhook's
    config passes through no API schema, so the retrieval bounds are checked
    here; a tool-mode KB without a `tool_description` is not refused, since
    `query_knowledge` has a default one.
    """

    knowledge_base_id: UUID
    mode: KnowledgeRetrievalMode = KnowledgeRetrievalMode.AUTO
    priority: int = 0
    top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    tool_description: str | None = None  # LLM instruction (tool mode only)


# A skill's name is what the model passes to `load_skill`, so it is shaped like
# a tool name (#423).
SKILL_NAME_PATTERN = r"^[a-z_][a-z0-9_]*$"
SKILL_NAME_MAX = 64


class SkillRef(DomainModel):
    """A skill an agent may load (#424): the name of one of its project's
    skills, resolved to that skill's live published version when a call's
    pipeline is built."""

    skill: str


# How many skills a call may hold loaded at once (#427): loading one past the
# cap unloads the one loaded longest ago, in the same switch.
DEFAULT_MAX_LOADED_SKILLS = 3
MAX_LOADED_SKILLS_RANGE = (1, 10)


# A century: past it, a number of days is a typo for "keep it", which is null.
MAX_RETENTION_DAYS = 36500


def _retention_days(kind: str) -> Any:
    """The one definition of a retention field, for every model of it."""
    return Field(
        default=None,
        ge=1,
        le=MAX_RETENTION_DAYS,
        description=(
            f"Days a call keeps its {kind}, counted from the call's end. Null "
            "on a project keeps it for good; null on an agent takes the "
            "project's"
        ),
    )


class RetentionConfig(DomainModel):
    """How long a call's data is kept, in days from the call's end (#316,
    ADR-0024). On an agent each field overrides the project's for that kind;
    null takes the project's, and the project's null keeps it for good. The
    one model of the triple: the project setting and the agent block in the
    API subclass it, and the retention janitor plans with it."""

    recordings_days: int | None = _retention_days(
        "recording (the audio file in object storage, and the carrier's copy "
        "if it kept one)"
    )
    transcripts_days: int | None = _retention_days(
        "transcript: what was said, the tool calls made from it, and the "
        "post-call analysis and takeaways"
    )
    events_days: int | None = _retention_days(
        "other events (the rest of the call's event log)"
    )

    def days(self, kind: str) -> int | None:
        """Days for one kind, as the janitor names them: `recording`,
        `transcript` or `events`."""
        return {
            "recording": self.recordings_days,
            "transcript": self.transcripts_days,
            "events": self.events_days,
        }[kind]

    def over(self, base: "RetentionConfig") -> "RetentionConfig":
        """This override on `base`: each kind set here wins."""
        return RetentionConfig(
            **{
                name: value if value is not None else getattr(base, name)
                for name, value in self.as_payload().items()
            }
        )

    @property
    def empty(self) -> bool:
        return all(value is None for value in self.as_payload().values())

    def as_payload(self) -> dict[str, int | None]:
        """As `call.ended` states it."""
        return {
            "recordings_days": self.recordings_days,
            "transcripts_days": self.transcripts_days,
            "events_days": self.events_days,
        }


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
    # How long the caller may stay quiet after the agent stops speaking before
    # the idle guard reacts. Distinct from silence_timeout_ms, which is the VAD
    # stop window inside a turn. 0 disables. See CONTEXT.md, "the three
    # timeouts".
    user_idle_timeout_ms: int = 10000
    idle_message: str = "Are you still there?"
    interruption_enabled: bool = True
    # False: the caller is not heard until the agent's first reply has played,
    # so a noise at connect cannot cut the greeting off. Cascade only.
    greeting_interruptible: bool = True
    smart_turn_detection: bool = True  # Use ML-based turn detection (LocalSmartTurnV3)
    smart_turn_stop_secs: float = 1.0  # Max silence before forcing end-of-turn
    max_call_duration_seconds: int = 3600
    voicemail_detection: VoicemailConfig = Field(
        default_factory=lambda: VoicemailConfig()
    )
    s2s: S2SConfig = Field(default_factory=S2SConfig)
    aws: AWSConfig = Field(default_factory=AWSConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    ambience: AmbienceConfig | None = None
    # A stored agent's override of its project's retention (#316). Read by the
    # janitor from the agent's live version, so it covers every version's
    # calls; an inline agent's is ignored (its calls take the project's).
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    @field_validator("ambience", mode="wrap")
    @classmethod
    def _unusable_ambience_is_off(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> AmbienceConfig | None:
        """Ambience can never end a call (ADR-0021). A stored agent's block
        passed the API schema, but an inline one arrives from a call-init
        webhook — a malformed block there drops the room, not the call."""
        try:
            return handler(value)  # type: ignore[no-any-return]
        except ValidationError as exc:
            logger.warning(
                "ambience_skipped: unusable ambience block, call runs without "
                "ambience: {err}",
                err=str(exc),
            )
            return None

    # An inline agent's knowledge bases (#260): it has no row to link them
    # to (ADR-0017). Read only for a call with no agent row, resolved against
    # that call's project; a stored agent's come from its links alone.
    knowledge_bases: list[KnowledgeBaseAttachment] = Field(default_factory=list)

    @field_validator("knowledge_bases", mode="before")
    @classmethod
    def _unusable_knowledge_bases_are_skipped(
        cls, value: Any
    ) -> list[KnowledgeBaseAttachment]:
        """An unusable entry drops that knowledge base, never the call — the
        config arrives from a call-init webhook as ambience's does."""
        if value is None:
            return []
        if not isinstance(value, list):
            logger.warning(
                "knowledge_base_skipped: knowledge_bases is not a list, call "
                "runs without them"
            )
            return []
        usable = []
        for entry in value:
            try:
                usable.append(KnowledgeBaseAttachment.model_validate(entry))
            except ValidationError as exc:
                logger.warning(
                    "knowledge_base_skipped: unusable knowledge_bases entry, call "
                    "runs without it: {err}",
                    err=str(exc),
                )
        return usable

    # The skills the agent may load mid-call (#424). Resolved once per call,
    # against the call's project; one that is missing or unpublished is a
    # warning and a skipped skill, never a failed call.
    skills: list[SkillRef] = Field(default_factory=list)

    @field_validator("skills", mode="before")
    @classmethod
    def _unusable_skills_are_skipped(cls, value: Any) -> list[SkillRef]:
        """An inline agent's config arrives from a call-init webhook, past any
        API schema: a malformed entry drops that skill, never the call."""
        if value is None:
            return []
        if not isinstance(value, list):
            logger.warning(
                "skill_skipped: skills is not a list, call runs without them"
            )
            return []
        usable = []
        for entry in value:
            try:
                usable.append(SkillRef.model_validate(entry))
            except ValidationError as exc:
                logger.warning(
                    "skill_skipped: unusable skills entry, call runs without it: {err}",
                    err=str(exc),
                )
        return usable

    # At most this many loaded at once (#427); the one loaded longest ago
    # makes room for the next.
    max_loaded_skills: int = DEFAULT_MAX_LOADED_SKILLS

    @field_validator("max_loaded_skills", mode="before")
    @classmethod
    def _unusable_cap_is_the_default(cls, value: Any) -> int:
        """Past the API schema (a call-init inline agent), a cap that is not a
        whole number is the default and one out of range is clamped into it:
        a bad cap must not fail the call."""
        low, high = MAX_LOADED_SKILLS_RANGE
        if value is None:
            return DEFAULT_MAX_LOADED_SKILLS
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning(
                "max_loaded_skills_unusable: {value!r}, the call uses {default}",
                value=value,
                default=DEFAULT_MAX_LOADED_SKILLS,
            )
            return DEFAULT_MAX_LOADED_SKILLS
        if not low <= value <= high:
            logger.warning(
                "max_loaded_skills_clamped: {value} is outside {low}-{high}",
                value=value,
                low=low,
                high=high,
            )
        return min(max(value, low), high)

    transport: str = "twilio"  # "twilio" | "webrtc" | "both"
    server_url: ServerUrlConfig = Field(default_factory=ServerUrlConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
