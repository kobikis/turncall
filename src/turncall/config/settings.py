"""Application settings loaded from environment variables."""

from enum import StrEnum
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class OpenRouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENROUTER_")

    api_key: str = Field(default="", alias="OPENROUTER_API_KEY")


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


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_")

    stdio_enabled: bool = Field(default=False, alias="MCP_STDIO_ENABLED")
    stdio_allowed_commands: list[str] = Field(
        default_factory=lambda: ["python", "node", "npx", "uvx"],
        alias="MCP_STDIO_ALLOWED_COMMANDS",
    )
    max_tools_per_server: int = Field(default=50, alias="MCP_MAX_TOOLS_PER_SERVER")
    max_response_bytes: int = Field(default=1_048_576, alias="MCP_MAX_RESPONSE_BYTES")


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
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    whatsapp: WhatsAppSettings = Field(default_factory=WhatsAppSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    byom: BYOMSettings = Field(default_factory=BYOMSettings)
    pipecat: PipecatSettings = Field(default_factory=PipecatSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
