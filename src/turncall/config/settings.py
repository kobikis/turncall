"""Application settings loaded from environment variables."""

from enum import StrEnum
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from turncall.domain.enums import EvalModelProvider

# Load .env before settings construction (nested models need os.environ)
load_dotenv()


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    url: PostgresDsn = Field(
        alias="DATABASE_URL",
        default="postgresql+asyncpg://turncall:turncall@localhost:5432/turncall",
    )
    pool_size: int = Field(default=20, alias="DATABASE_POOL_SIZE")
    max_overflow: int = Field(default=10, alias="DATABASE_MAX_OVERFLOW")
    pool_timeout: int = Field(default=30, alias="DATABASE_POOL_TIMEOUT")
    echo: bool = Field(default=False, alias="DATABASE_ECHO")


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    url: RedisDsn = Field(
        alias="REDIS_URL",
        default="redis://localhost:6379/0",
    )
    max_connections: int = Field(default=50, alias="REDIS_MAX_CONNECTIONS")


class TwilioSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TWILIO_")

    account_sid: str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    auth_token: str = Field(default="", alias="TWILIO_AUTH_TOKEN")


class OpenAISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: str = Field(default="", alias="OPENAI_API_KEY")
    stt_model: str = "whisper-1"
    llm_model: str = "gpt-4o"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"


class GoogleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_")

    api_key: str = Field(default="", alias="GOOGLE_API_KEY")


class AnthropicSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANTHROPIC_")

    api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")


class CartesiaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CARTESIA_")

    api_key: str = Field(default="", alias="CARTESIA_API_KEY")


class AssemblyAISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASSEMBLYAI_")

    api_key: str = Field(default="", alias="ASSEMBLYAI_API_KEY")


class DeepgramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEEPGRAM_")

    api_key: str = Field(default="", alias="DEEPGRAM_API_KEY")


class ElevenLabsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ELEVENLABS_")

    api_key: str = Field(default="", alias="ELEVENLABS_API_KEY")


class HeyGenSettings(BaseSettings):
    """A LiveAvatar key, not a HeyGen one — pipecat targets api.liveavatar.com."""

    model_config = SettingsConfigDict(env_prefix="HEYGEN_")

    api_key: str = Field(default="", alias="HEYGEN_LIVE_AVATAR_API_KEY")


class TavusSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAVUS_")

    api_key: str = Field(default="", alias="TAVUS_API_KEY")


class CredentialSettings(BaseSettings):
    """Project credentials at rest (ADR-0020)."""

    model_config = SettingsConfigDict(env_prefix="")

    # Fernet key. Set once and never rotate: rotating orphans every stored
    # credential. Unset while credentials exist is a startup error, never a
    # silent fall back to plaintext.
    encryption_key: str = Field(default="", alias="CREDENTIAL_ENCRYPTION_KEY")


class OpenRouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENROUTER_")

    api_key: str = Field(default="", alias="OPENROUTER_API_KEY")


class TypeSafeSettings(BaseSettings):
    """Jev, TypeSafe's classification model (#141).

    Evals only. No live call contacts this vendor, and the dependency is an
    optional extra (`turncall[evals-jev]`) for that reason.
    """

    model_config = SettingsConfigDict(env_prefix="TYPESAFE_")

    api_key: str = Field(default="", alias="TYPESAFE_API_KEY")


class WhatsAppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WHATSAPP_")

    token: str = Field(default="", alias="WHATSAPP_TOKEN")
    phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    webhook_verify_token: str = Field(default="", alias="WHATSAPP_WEBHOOK_VERIFY_TOKEN")


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    backend: str = Field(default="local", alias="STORAGE_BACKEND")  # "local" or "s3"
    local_path: str = Field(default="./storage", alias="LOCAL_STORAGE_PATH")
    s3_bucket: str = Field(default="turncall-artifacts", alias="S3_BUCKET_NAME")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    # Where a call's ambience renditions are fetched to, since the mixer takes a
    # path and storage returns bytes (ADR-0021). Mount it: under a container's
    # own filesystem every recreate re-fetches every sound, the lesson
    # EVAL_TTS_CACHE_DIR already paid for.
    ambience_cache_dir: str = Field(
        default="./storage/ambience-cache", alias="AMBIENCE_CACHE_DIR"
    )


class AWSSettings(BaseSettings):
    """AWS settings for the `bedrock` LLM and `aws` S2S providers (ADR-0016)."""

    model_config = SettingsConfigDict(env_prefix="")

    region: str = Field(default="us-east-1", alias="AWS_REGION")
    # Per-agent *static* AWS keys are persisted in config_blob (unencrypted
    # JSONB) and are long-lived by construction, so operators opt in
    # deliberately. Agents supplying them are rejected at create when off.
    # Governs AGENT CONFIG ONLY: a project credential (ADR-0020) is encrypted
    # and outside that surface, so false does not mean "no static AWS keys on
    # this deployment" — it means "none inside agent config".
    agent_credentials_enabled: bool = Field(
        default=False, alias="AWS_AGENT_CREDENTIALS_ENABLED"
    )


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_")

    stdio_enabled: bool = Field(default=False, alias="MCP_STDIO_ENABLED")
    stdio_allowed_commands: list[str] = Field(
        default_factory=lambda: ["python", "node", "npx", "uvx"],
        alias="MCP_STDIO_ALLOWED_COMMANDS",
    )
    max_tools_per_server: int = Field(default=50, alias="MCP_MAX_TOOLS_PER_SERVER")
    # Per-server caps don't compose: ten servers at the per-server limit would
    # put 500 tools in every request. This is the ceiling across all of them.
    max_tools_total: int = Field(default=100, alias="MCP_MAX_TOOLS_TOTAL")
    max_response_bytes: int = Field(default=1_048_576, alias="MCP_MAX_RESPONSE_BYTES")
    # Whole-discovery budget. A caller is listening to silence while this runs,
    # and only the HTTP and SSE transports carry a timeout of their own — stdio
    # has none, so a subprocess that reads its input and never answers held the
    # call open with nothing to hear.
    connect_timeout_seconds: float = Field(
        default=10.0, alias="MCP_CONNECT_TIMEOUT_SECONDS"
    )


class ToolSettings(BaseSettings):
    """Limits on custom webhook tools. MCP has its own under MCPSettings."""

    model_config = SettingsConfigDict(env_prefix="TOOL_")

    # A tool result goes into the prompt and stays there for the rest of the
    # conversation, so an oversized one is charged on every subsequent turn
    # before it eventually ends the call on context length. Same default and
    # behaviour as MCP_MAX_RESPONSE_BYTES, which capped only half the tools;
    # webhook tools are the half customers actually write.
    max_response_bytes: int = Field(default=1_048_576, alias="TOOL_MAX_RESPONSE_BYTES")


class LimitSettings(BaseSettings):
    """The platform's default limits (#206), 0 = unlimited. A project's own
    value (`PUT /v1/projects/{id}/limits`) wins over these, in either direction:
    a default, not a ceiling."""

    model_config = SettingsConfigDict(env_prefix="LIMIT_")

    # Live calls one project may hold at once, across every server.
    live_calls: int = Field(default=0, ge=0, alias="LIMIT_LIVE_CALLS")
    # Outbound calls each of a project's numbers may start per second.
    outbound_calls_per_second: int = Field(
        default=0, ge=0, alias="LIMIT_OUTBOUND_CALLS_PER_SECOND"
    )
    # Inbound messages one text session (chat, SMS, WhatsApp) may send a minute.
    messages_per_minute: int = Field(default=0, ge=0, alias="LIMIT_MESSAGES_PER_MINUTE")


class CampaignSettings(BaseSettings):
    """The campaign worker (#229)."""

    model_config = SettingsConfigDict(env_prefix="CAMPAIGN_")

    # Longest the worker sleeps between passes. A new campaign wakes it at once
    # (its id is pushed on a Redis list); this bounds how late it notices
    # everything else — a call ending and freeing a slot, a target coming due.
    poll_seconds: int = Field(default=2, ge=1, alias="CAMPAIGN_POLL_SECONDS")


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8090, alias="PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    # Public https base URL (e.g. https://abc.ngrok.io) for Twilio callbacks issued
    # without an inbound request to derive the host from — warm-transfer whisper,
    # transfer-result, and AMD callbacks. See ADR-0009.
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    signing_secret: str = Field(default="", alias="WEBHOOK_SIGNING_SECRET")
    max_retries: int = 5
    retry_base_delay_seconds: float = 1.0


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    api_key_hash_secret: str = Field(
        default="change-me-in-production",
        alias="API_KEY_HASH_SECRET",
    )
    # Privileged credential that gates the unauthenticated bootstrap endpoints
    # (project + first-key creation). Only the builder holds it. Empty by default
    # so the gate fails closed — there is no value that works in production unset.
    platform_api_key: str = Field(default="", alias="PLATFORM_API_KEY")


class BYOMSettings(BaseSettings):
    """Bring Your Own Model settings for custom LLM providers."""

    model_config = SettingsConfigDict(env_prefix="BYOM_")

    enabled: bool = True
    allowed_url_patterns: list[str] = Field(default_factory=list)


class PipecatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PIPECAT_")

    vad_enabled: bool = True
    vad_confidence_threshold: float = 0.6
    audio_sample_rate: int = 8000
    audio_channels: int = 1
    session_timeout_seconds: int = 3600
    enable_metrics: bool = True
    # Observability (ADR-0010). Both on by default in every environment; the
    # hot-path cost is contained (async log sink; tracing never console-exports in
    # prod and self-disables there without an OTLP endpoint).
    enable_observers: bool = True
    enable_tracing: bool = True
    # Put from_number/to_number on spans (PII in the tracing backend). Flip off for
    # compliance-sensitive deployments; non-PII attrs are always present.
    trace_include_pii: bool = True
    otel_service_name: str = "turncall"


class EvalSettings(BaseSettings):
    """Agent evals (#68, ADR-0018)."""

    model_config = SettingsConfigDict(env_prefix="EVAL_")

    # How many scenario-iterations the worker runs at once. The worker runs the
    # bot pipeline *and* the harness -- which itself runs a persona LLM, a TTS,
    # an STT and the judge -- so in audio mode one run is roughly double a real
    # call's service load in a single event loop. 4 is a starting point, not a
    # measurement.
    max_concurrent_runs: int = 4
    # The janitor's cutoff: a run still `running` past this is a crashed worker,
    # not slow work, and is swept to `errored`.
    max_run_duration_seconds: int = 900
    # The same for a run nothing ever claimed — the API committed the row and
    # then died before the queue push, or Redis lost the list. Longer, because
    # waiting behind a backlog is normal where running for 15 minutes is not.
    max_queued_seconds: int = 3600
    janitor_interval_seconds: int = 60
    # Ceiling on iterations per run, so one request cannot queue an unbounded
    # amount of paid LLM work.
    max_iterations: int = 50
    # The other axis of the same bill (#97). A `tag` fans out to every scenario
    # carrying it, so without this one POST queues `scenarios x iterations`
    # full conversations — each an agent pipeline plus a persona LLM plus a
    # judge. Over the cap the request is refused, never truncated: a batch that
    # silently ran 50 of 200 scenarios reports a pass for a suite that never
    # ran.
    max_scenarios_per_request: int = 50
    # The judge and the persona, set once for the whole platform (#119).
    # Precedence is narrowest-wins: a scenario's own block, then these, then
    # pipecat's local Ollama. A block here is taken whole — a scenario that
    # names a judge keeps the one it names, rather than having a platform model
    # merged into a provider that does not offer it.
    #
    # Unset provider + a model or temperature means "the default provider,
    # configured": `compile_model_block` fills in ollama, which is what pipecat
    # would have run anyway.
    judge_provider: EvalModelProvider | None = None
    judge_model: str | None = None
    judge_temperature: float | None = None
    simulator_provider: EvalModelProvider | None = None
    simulator_model: str | None = None
    simulator_temperature: float | None = None

    @field_validator("simulator_provider")
    @classmethod
    def _persona_must_be_an_llm(
        cls, value: EvalModelProvider | None
    ) -> EvalModelProvider | None:
        """A persona is linked into a pipeline to speak, so a classifier cannot
        be one (#141) — the provider set is shared with the judge, which may be.

        Refused at startup rather than per run: the alternative is every
        simulation on the platform erroring with nothing saying the setting is
        the cause.
        """
        from turncall.evals.judges import CLASSIFIER_ONLY_JUDGES

        if value is not None and value.value in CLASSIFIER_ONLY_JUDGES:
            raise ValueError(
                f"EVAL_SIMULATOR_PROVIDER cannot be {value.value!r}: it is a "
                "classifier, and the persona is an LLM linked into a pipeline "
                "to speak. It is available as EVAL_JUDGE_PROVIDER."
            )
        return value

    # Where pipecat's caching TTS keeps the caller's synthesized turns (#72),
    # keyed by service/voice/model/language/speed/text. Pipecat's own default
    # is under $HOME, which a container loses on every recreate — a scripted
    # scenario would then re-synthesize every caller turn on every run. Point
    # it at the same place the app already persists things, and mount it.
    tts_cache_dir: str = "./storage/eval-tts-cache"


class Settings(BaseSettings):
    """Root settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        alias="TURNCALL_ENV",
    )
    debug: bool = Field(default=False, alias="DEBUG")

    # Soft-deleted projects (ADR-0011) are hard-deleted by the purge job this
    # many days after deletion. 0 disables the purge (soft-deletes kept forever).
    project_purge_retention_days: int = Field(
        default=30, alias="PROJECT_PURGE_RETENTION_DAYS"
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    twilio: TwilioSettings = Field(default_factory=TwilioSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    cartesia: CartesiaSettings = Field(default_factory=CartesiaSettings)
    assemblyai: AssemblyAISettings = Field(default_factory=AssemblyAISettings)
    deepgram: DeepgramSettings = Field(default_factory=DeepgramSettings)
    elevenlabs: ElevenLabsSettings = Field(default_factory=ElevenLabsSettings)
    heygen: HeyGenSettings = Field(default_factory=HeyGenSettings)
    tavus: TavusSettings = Field(default_factory=TavusSettings)
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    typesafe: TypeSafeSettings = Field(default_factory=TypeSafeSettings)
    whatsapp: WhatsAppSettings = Field(default_factory=WhatsAppSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    byom: BYOMSettings = Field(default_factory=BYOMSettings)
    aws: AWSSettings = Field(default_factory=AWSSettings)
    pipecat: PipecatSettings = Field(default_factory=PipecatSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    evals: EvalSettings = Field(default_factory=EvalSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    campaigns: CampaignSettings = Field(default_factory=CampaignSettings)

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
