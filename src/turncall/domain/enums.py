"""Domain enumerations."""

from enum import StrEnum


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    TRANSFERRING = "transferring"
    HANDED_OFF = "handed_off"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"


class EndedReason(StrEnum):
    """Granular reason a call ended — derived for call.ended. See ADR-0008."""

    VOICEMAIL = "voicemail"
    TRANSFERRED = "transferred"
    ASSISTANT_ENDED_CALL = "assistant_ended_call"
    CUSTOMER_DID_NOT_ANSWER = "customer_did_not_answer"
    CUSTOMER_BUSY = "customer_busy"
    PIPELINE_ERROR = "pipeline_error"
    TELEPHONY_FAILED = "telephony_failed"
    CUSTOMER_ENDED_CALL = "customer_ended_call"
    UNKNOWN = "unknown"


class AgentState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"  # Superseded by a newer published version


class ToolExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ToolExecutionMode(StrEnum):
    SYNC = "sync"
    ASYNC = "async"


class TransferMode(StrEnum):
    WARM = "warm"
    COLD = "cold"


class RecordingStatus(StrEnum):
    NONE = "none"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class RoutingTargetType(StrEnum):
    ASSISTANT = "assistant"
    WORKFLOW = "workflow"
    WEBHOOK = "webhook"


class CallEventType(StrEnum):
    # Call lifecycle
    CALL_INITIALIZING = "call.initializing"
    CALL_STARTED = "call.started"
    CALL_ENDED = "call.ended"
    CALL_FAILED = "call.failed"
    CALL_TRANSFERRED = "call.transferred"
    TRANSFER_ANSWERED = "transfer.answered"
    CALL_AGENT_HANDOFF = "call.agent_handoff"

    # Transcripts
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"

    # Tools
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"

    # Recordings
    RECORDING_READY = "recording.ready"

    # Analysis
    # Reserved, NOT dispatched — analysis ships inline in call.ended. See adr/0007.
    ANALYSIS_COMPLETED = "analysis.completed"

    # Errors
    ERROR_RAISED = "error.raised"

    # SMS / Chat
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_DELETED = "session.deleted"
    CHAT_CREATED = "chat.created"

    # Live control
    CONTEXT_INJECTED = "context.injected"
    DTMF_SENT = "dtmf.sent"
    PLAYBACK_MUTED = "playback.muted"
    PLAYBACK_UNMUTED = "playback.unmuted"
    RECORDING_PAUSED = "recording.paused"
    RECORDING_RESUMED = "recording.resumed"


class PipelineMode(StrEnum):
    CASCADE = "cascade"
    S2S = "s2s"


class ProjectRole(StrEnum):
    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        """Privilege ordering (higher = more). Used to forbid a caller from
        granting a role above their own when creating API keys."""
        return {"viewer": 1, "developer": 2, "admin": 3}[self.value]


class SmsSessionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class SmsMessageRole(StrEnum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatChannel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    WEB = "web"
    API = "api"


class DocumentStatus(StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeRetrievalMode(StrEnum):
    AUTO = "auto"
    TOOL = "tool"
    PROMPT = "prompt"
