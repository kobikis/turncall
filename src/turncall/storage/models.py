"""SQLAlchemy ORM models (database tables)."""

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    type_annotation_map: ClassVar[dict[type, type]] = {
        dict: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    # Soft delete (ADR-0011): rows stay for history; a purge job hard-deletes
    # later. Set => the project is gone (auth rejects its keys, GET 404s).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Limits (#206), null = the platform default (LIMIT_* env, unlimited
    # unless set). Enforced in Redis by `services/limits`, not here.
    limit_live_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_outbound_calls_per_second: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    limit_messages_per_minute: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Redaction (#313, ADR-0024): off unless an admin turns it on. Entities
    # null = the platform default list (`services/redaction.DEFAULT_ENTITIES`).
    # Applied at the event exit point (`events/emit.py`), not here.
    redaction_enabled: Mapped[bool] = mapped_column(
        default=False, server_default=text("false")
    )
    redaction_entities: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Retention (#316, ADR-0024), in days from the call's end; null = kept for
    # good, the default. An agent's `retention` overrides each one. Enforced
    # by the janitor (`services/retention`), not here.
    retention_recordings_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    retention_transcripts_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    retention_events_days: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("admin", "developer", "viewer", name="project_role"), nullable=False
    )
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_api_keys_project_id", "project_id"),)


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(50), nullable=False, default="development"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
    )
    config_blob: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_agents_project_env", "project_id", "environment"),
        Index(
            "ix_agents_project_name_version",
            "project_id",
            "name",
            "version",
            unique=True,
        ),
    )


class PhoneNumberRow(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="twilio")
    external_number_sid: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    e164_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    routing_target_type: Mapped[str] = mapped_column(
        Enum("agent", "workflow", "webhook", name="routing_target_type"),
        nullable=False,
    )
    routing_target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    server_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_url_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sms_enabled: Mapped[bool] = mapped_column(default=False)
    whatsapp_enabled: Mapped[bool] = mapped_column(default=False)
    routing_weights: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # The number's own Twilio account (services/twilio_accounts.py). Null = the
    # platform's TWILIO_* account. The SID is an identifier (webhooks are matched
    # by it); the token is Fernet ciphertext, never returned by any read.
    twilio_account_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    twilio_auth_token_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_phone_numbers_project_id", "project_id"),
        Index("ix_phone_numbers_twilio_account_sid", "twilio_account_sid"),
    )


class CallRow(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="twilio")
    provider_call_sid: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    parent_provider_call_sid: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    direction: Mapped[str] = mapped_column(
        Enum("inbound", "outbound", name="call_direction"), nullable=False
    )
    from_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    active_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Enum(
            "initiated",
            "ringing",
            "in_progress",
            "transferring",
            "handed_off",
            "completed",
            "failed",
            "no_answer",
            "busy",
            "voicemail",
            name="call_status",
        ),
        nullable=False,
        default="initiated",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_status: Mapped[str] = mapped_column(
        Enum(
            "none",
            "in_progress",
            "paused",
            "completed",
            "failed",
            name="recording_status",
        ),
        nullable=False,
        default="none",
    )
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    analysis_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # When a carrier media stream was attached to the call: one per call, ever
    # (`call_repo.attach_media`). Internal — on no read and in no webhook.
    media_attached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Retention (#316): when the janitor deleted each kind of the call's data
    # (`services/retention`), null = not yet. The row itself is never deleted.
    recording_expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transcript_expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    events_expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Derived from the events while they exist (`services/call_view`); frozen
    # here when they expire, so the read still says how the call ended.
    ended_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_calls_project_id", "project_id"),
        Index("ix_calls_project_status", "project_id", "status"),
        # GET /v1/calls filters project_id and sorts created_at DESC — this
        # composite serves both, so an append-forever table stays fast.
        Index("ix_calls_project_created", "project_id", "created_at"),
        # The retention janitor's sweep, one per kind: only calls whose data of
        # that kind is still there (#316).
        *(
            Index(
                f"ix_calls_{kind}_retention",
                "project_id",
                "created_at",
                postgresql_where=text(f"{kind}_expired_at IS NULL"),
            )
            for kind in ("recording", "transcript", "events")
        ),
    )


class ToolInvocationRow(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    # A row belongs to a voice call or to a text session, never both and never
    # neither — the CHECK below is what makes that true rather than a comment.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sms_sessions.id", ondelete="CASCADE"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "pending", "running", "succeeded", "failed", "timed_out", name="tool_status"
        ),
        nullable=False,
        default="pending",
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_tool_invocations_call_id", "call_id"),
        Index("ix_tool_invocations_session_id", "session_id"),
        CheckConstraint(
            "num_nonnulls(call_id, session_id) = 1",
            name="ck_tool_invocations_owner",
        ),
    )


class CallEventRow(Base):
    __tablename__ = "call_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    internal_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_call_events_call_id", "call_id"),
        Index("ix_call_events_call_type", "call_id", "event_type"),
        Index(
            "ix_call_events_call_seq",
            "call_id",
            "sequence_number",
            unique=True,
        ),
    )


class WebhookSubscriptionRow(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (Index("ix_webhook_subs_project_id", "project_id"),)


class SmsSessionRow(Base):
    __tablename__ = "sms_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Null for an inline agent from call-init (ADR-0017); its config is
    # `metadata_json["dynamic_config"]`.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    customer_number: Mapped[str] = mapped_column(String(20), nullable=False)
    turncall_number: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    channel: Mapped[str] = mapped_column(String(10), nullable=False, default="sms")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_sms_sessions_project_id", "project_id"),
        Index(
            "ix_sms_sessions_lookup",
            "customer_number",
            "turncall_number",
            "status",
        ),
        Index("ix_sms_sessions_expires_at", "expires_at"),
        # The redaction sweep's claim (#314, sms_session_repo
        # .claim_pending_redaction): the sessions still to be rewritten.
        Index(
            "ix_sms_sessions_redaction_pending",
            "expires_at",
            postgresql_where=text("metadata_json ->> 'redaction' = 'pending'"),
        ),
    )


class SmsMessageRow(Base):
    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sms_sessions.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False, default="sms")
    provider_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_sms_messages_session_id", "session_id"),
        Index("ix_sms_messages_session_created", "session_id", "created_at"),
    )


class KnowledgeBaseRow(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="text-embedding-3-small"
    )
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=512)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=64)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("ix_knowledge_bases_project_id", "project_id"),
        Index(
            "ix_knowledge_bases_project_name",
            "project_id",
            "name",
            unique=True,
        ),
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Up to ~1MB/row and read only by prompt-mode retrieval — deferred so
    # document list/detail queries don't drag it into memory. Readers that need
    # it undefer explicitly (knowledge_repo.get_all_documents_text).
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="processing"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (Index("ix_documents_knowledge_base_id", "knowledge_base_id"),)


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding = mapped_column(
        __import__("pgvector.sqlalchemy", fromlist=["Vector"]).Vector(1536),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_knowledge_base_id", "knowledge_base_id"),
    )


class AgentKnowledgeBaseRow(Base):
    __tablename__ = "agent_knowledge_bases"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    similarity_threshold: Mapped[float] = mapped_column(nullable=False, default=0.3)
    tool_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        Index("ix_agent_kb_agent_id", "agent_id"),
        Index("ix_agent_kb_knowledge_base_id", "knowledge_base_id"),
    )


class AmbienceSoundRow(Base):
    """A project's uploaded ambience sound (ADR-0021).

    The uploaded bytes are not kept: what plays is a mono MP3 rendition per
    voice transport rate, made at upload. `renditions` maps the rate (as a
    string — JSONB keys) to its object-storage key. Immutable once created, so
    a cached rendition never goes stale.
    """

    __tablename__ = "ambience_sounds"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    renditions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (Index("ix_ambience_sounds_project_id", "project_id"),)


class ProjectCredentialRow(Base):
    """A project's own key for one provider account (ADR-0020).

    One row per (project, account) — never per role. `ciphertext` is a Fernet
    token over a JSON document: {"api_key": ...}, or for `aws` the AWSConfig
    shape. Never returned by any read.
    """

    __tablename__ = "project_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "provider", name="uq_project_credentials_project_provider"
        ),
    )


class TakeawayRow(Base):
    """A reusable post-call structured-output definition (Takeaway, ADR-0013).

    Agents attach takeaways via analysis.takeaway_ids; after each call an LLM
    extracts JSON matching `schema` and the result ships inside the analysis.
    """

    __tablename__ = "takeaways"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("ix_takeaways_project_id", "project_id"),
        UniqueConstraint("project_id", "name", name="uq_takeaways_project_name"),
    )


class EvalScenarioRow(Base):
    """One saved behavioural test: a scripted conversation or a simulation.

    `definition` is pipecat's own scenario mapping, stored verbatim. Columns
    would mean tracking pipecat's schema in Alembic forever, and that schema
    moves between majors — so it is validated at the API boundary by
    round-tripping through pipecat's parser and then stored as given, with
    `schema_version` recording which schema it targets.

    `tool_mocks` and `tool_policy` are deliberately TurnCall columns rather
    than keys inside `definition`: mixing ownership would mean a future pipecat
    migration had to preserve foreign keys inside a mapping it does not own.
    """

    __tablename__ = "eval_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(
        Enum("script", "simulation", name="eval_kind"), nullable=False
    )
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_mocks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tool_policy: Mapped[str] = mapped_column(
        Enum("mock_only", "live", name="eval_tool_policy"),
        nullable=False,
        default="mock_only",
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    default_target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The LLMs an eval runs besides the agent's (#118): the judge that decides
    # `eval:` assertions and every simulation verdict, and the persona that
    # plays the caller. TurnCall columns for the reason above — and because the
    # pipecat block they compile into names a `factory`, a dotted path this
    # service imports, which must never come from a request.
    judge: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    simulator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        Index("ix_eval_scenarios_project", "project_id"),
        Index("ix_eval_scenarios_tags", "tags", postgresql_using="gin"),
        UniqueConstraint("project_id", "name", name="uq_eval_scenarios_project_name"),
    )


class EvalRunRow(Base):
    """One scenario x target x modality, executed over N iterations.

    Three snapshots make a finished run interpretable after the fact: the agent
    config that actually ran, the scenario as it stood, and the harness config
    (judge model, pipecat version). The agent may be edited or archived, the
    scenario and its mocks may be edited, and the judge model is what decided
    the verdict — this is ADR-0017's snapshot rule applied one level out.

    There is no score column. A scripted scenario yields pass/fail and a
    simulation a rate over N iterations; `passed_count`/`failed_count` out of
    `iterations` represents both honestly (1/1, or 7/10) with no column meaning
    two incomparable things.
    """

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # SET NULL, not CASCADE: deleting a scenario must not destroy the history of
    # what it once proved. scenario_name is why the row still reads.
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eval_scenarios.id", ondelete="SET NULL"), nullable=True
    )
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum("script", "simulation", name="eval_kind"), nullable=False
    )
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resolved_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    resolved_scenario: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    harness_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # NULL for an inline target — ADR-0017's first rule: no locally invented
    # sentinel in the column.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Which version `agent_id` was, recorded because the column carries no
    # foreign key: the agent row can be deleted and take the answer with it.
    # NULL for an inline target, which has no version (#74).
    agent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modality: Mapped[str] = mapped_column(
        Enum("text", "audio", name="eval_modality"), nullable=False
    )
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        Enum(
            "queued",
            "running",
            "passed",
            "failed",
            "errored",
            "cancelled",
            name="eval_run_status",
        ),
        nullable=False,
        default="queued",
    )
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Things the run's author needs to know that are not verdicts (#96): a mock
    # keyed to a tool this run can never call, an agent whose real tools are
    # allowed to fire. They used to be worker log lines, which is a container
    # nobody watching a run ever reads.
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_eval_runs_project_queued", "project_id", "queued_at"),
        Index("ix_eval_runs_batch", "batch_id"),
        Index("ix_eval_runs_scenario", "scenario_id"),
    )


class SuppressedNumberRow(Base):
    """A number on a project's suppression list: never dialled (#227).

    Checked by `services/outbound_dial`, the one path every outbound call takes.
    Holds no consent record — consent is the operator's, not the platform's.
    """

    __tablename__ = "suppressed_numbers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    e164_number: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    __table_args__ = (
        # Also the index the per-dial lookup uses.
        UniqueConstraint(
            "project_id", "e164_number", name="uq_suppressed_numbers_project_number"
        ),
    )


class CampaignRow(Base):
    """A set of outbound calls one agent places to a list of targets (#229).

    `agent_id` and `from_number_id` carry no foreign key, like an eval run's:
    they are checked at create, and a row deleted since fails its targets'
    dials by name instead of blocking the delete or taking the campaign with it.
    """

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Null for a weighted campaign (#234), whose `agent_weights` —
    # `[{agent_id, weight}]`, a phone number's `routing_weights` shape — assign
    # each target its agent at create.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_weights: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    from_number_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # The default for targets that name none; each target stores its own.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # `{start, end, days}` in each target's own zone (#230); null = any hour.
    calling_window: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # `{ended_reason: {max_attempts, spacing_seconds}}` (#231); null = no retries.
    retry_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # A string, not a PG enum: #232 adds states, and a column of strings takes
    # them without an `ALTER TYPE` that cannot run inside a transaction.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_project", "project_id"),
    )


class CampaignTargetRow(Base):
    """One number a campaign dials, with the variables its agent is given.

    `timezone` is resolved at create (the target's own, else the campaign's)
    and never inferred from the number: a ported mobile keeps its area code.
    `next_attempt_at` null means due now; a calling window (#230) or a retry's
    spacing (#231) sets it. `attempts` counts dials actually placed. A target
    waiting on a retry is `pending` with the last attempt's `ended_reason`.
    """

    __tablename__ = "campaign_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    e164_number: Mapped[str] = mapped_column(String(20), nullable=False)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # The agent that calls this target, assigned at create (#234): the
    # campaign's own, or its weights' pick for this number. No foreign key, like
    # the campaign's. Null only on a target from before #234.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The latest attempt's call; every attempt's call carries this target's id
    # in its metadata.
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    ended_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        # A number twice in one campaign is a person called twice.
        UniqueConstraint(
            "campaign_id", "e164_number", name="uq_campaign_targets_number"
        ),
        Index("ix_campaign_targets_claim", "campaign_id", "status", "position"),
        # The claim's two lookups and the janitor's sweep (see
        # campaign_repo.claim_statement / reclaim_abandoned).
        Index(
            "ix_campaign_targets_fresh",
            "campaign_id",
            "position",
            postgresql_where=text("status = 'pending' AND next_attempt_at IS NULL"),
        ),
        Index(
            "ix_campaign_targets_due",
            "campaign_id",
            "next_attempt_at",
            "position",
            postgresql_where=text("status = 'pending' AND next_attempt_at IS NOT NULL"),
        ),
        Index(
            "ix_campaign_targets_claimed",
            "status",
            "claimed_at",
            postgresql_where=text("status IN ('dialling', 'placing')"),
        ),
    )
