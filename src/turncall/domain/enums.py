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
    MAX_DURATION_REACHED = "max_duration_reached"
    CUSTOMER_SILENT = "customer_silent"
    CUSTOMER_ENDED_CALL = "customer_ended_call"
    UNKNOWN = "unknown"


class AgentState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"  # Superseded by a newer published version


# A skill version (#423) moves through an agent version's states, by the same
# rules: a draft is edited, publish makes it live and archives the previous
# live version of its name, rollback restores an archived one.
SkillState = AgentState


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
    # Deleted under the project's retention (#316): there was one, and it is gone.
    EXPIRED = "expired"


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
    CALL_MAX_DURATION_REACHED = "call.max_duration_reached"
    CALL_CUSTOMER_SILENT = "call.customer_silent"
    # The caller asked not to be called again, via the do_not_call tool (#228).
    CALL_OPTED_OUT = "call.opted_out"
    # Refused at the carrier over the project's live-call limit (#224). No call
    # row exists, so it is dispatched with no call_id and never stored.
    CALL_REJECTED = "call.rejected"

    # Transcripts
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"

    # Per-turn timings (#176). Stored, never dispatched as a webhook.
    METRICS_TURN = "metrics.turn"
    # One per gpt-live-1 delegation (#247). Stored, never dispatched.
    DELEGATION_COMPLETED = "delegation.completed"
    # Why a call's Avatar did not show (#402). Stored, never dispatched.
    AVATAR_UNAVAILABLE = "avatar.unavailable"

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
    # A provider stopped being usable and the call moved to its backup (#222).
    PROVIDER_FAILOVER = "provider.failover"

    # Skills (#425): a skill's instructions and tools added to the live call,
    # and taken off it again (#427's limit; nothing sends it yet). Stored and
    # dispatched; identifiers only (`{skill, version, reason}`).
    SKILL_LOADED = "skill.loaded"
    SKILL_UNLOADED = "skill.unloaded"

    # Procedures (#443): a move the agent reported, accepted
    # (`{procedure, from, to, kind}`, `kind` the step it reached) or refused
    # (`{procedure, from, to}`). Stored, never dispatched; identifiers only.
    PROCEDURE_STEP = "procedure.step"
    PROCEDURE_REFUSED = "procedure.refused"

    # SMS / Chat
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_DELETED = "session.deleted"
    CHAT_CREATED = "chat.created"

    # Evals (#76). Two, not a scatter: `eval.run.completed` is comprehensive
    # and carries the whole result, the way `call.ended` does (ADR-0006).
    # `analysis.completed` above is the counter-example — reserved, never
    # dispatched, and a standing source of "why does nothing arrive".
    EVAL_RUN_STARTED = "eval.run.started"
    EVAL_RUN_COMPLETED = "eval.run.completed"

    # Campaigns (#234): a pair on the eval pattern. `campaign.completed` carries
    # the campaign's read — status and target counts per status — and the
    # campaign's id rides in the envelope, never the payload. `cancelled` is
    # the other way a campaign ends (#232), so it gets its own terminal event
    # with the same payload rather than a `completed` that lies about why.
    CAMPAIGN_STARTED = "campaign.started"
    CAMPAIGN_COMPLETED = "campaign.completed"
    CAMPAIGN_CANCELLED = "campaign.cancelled"

    # Live control
    CONTEXT_INJECTED = "context.injected"
    DTMF_SENT = "dtmf.sent"
    PLAYBACK_MUTED = "playback.muted"
    PLAYBACK_UNMUTED = "playback.unmuted"
    RECORDING_PAUSED = "recording.paused"
    RECORDING_RESUMED = "recording.resumed"


class AvatarUnavailableReason(StrEnum):
    """Why a call's Avatar did not show: `avatar.unavailable`'s `reason` (#402).
    A closed set, read by consoles."""

    # The call cannot carry video: not a browser (WebRTC) call.
    TRANSPORT = "transport"
    # HeyGen with no avatar_id, Tavus with no replica_id.
    MISSING_ID = "missing_id"
    # A provider TurnCall has no Avatar for. The API refuses one; a call-init
    # inline Agent is not checked by it.
    UNKNOWN_PROVIDER = "unknown_provider"
    # The provider refused the session, or never put the Avatar in it.
    SESSION_REFUSED = "session_refused"
    # The session stopped mid-call (a sandbox cutoff, a dropped connection).
    SESSION_ENDED = "session_ended"


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


class AuditResource(StrEnum):
    """What an audit log entry is about (#317, ADR-0024)."""

    AGENT = "agent"
    CREDENTIAL = "credential"
    PHONE_NUMBER = "phone_number"
    API_KEY = "api_key"
    PROJECT = "project"
    SKILL = "skill"


class AuditAction(StrEnum):
    """What was done to the resource. `archived` is an agent version a
    publish or rollback took out of service; `deleted` is an agent deleted
    through the API (archived too, but asked for). `purchased` is a number
    bought on a carrier account (ADR-0025): its entry's resource id is the
    E.164 number, since a bought number has no binding yet. `released` is a
    number given back to its carrier (irreversible), keyed the same way: only
    an unbound number can be released."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"
    KNOWLEDGE_BASE_LINKED = "knowledge_base_linked"
    KNOWLEDGE_BASE_UNLINKED = "knowledge_base_unlinked"
    ROUTING_SET = "routing_set"
    ROUTING_CLEARED = "routing_cleared"
    REVOKED = "revoked"
    LIMITS_SET = "limits_set"
    REDACTION_SET = "redaction_set"
    RETENTION_SET = "retention_set"
    SKILL_LIBRARY_SET = "skill_library_set"
    PURCHASED = "purchased"
    RELEASED = "released"


class AuditActor(StrEnum):
    """Who made the change: a project's API key, or the platform credential
    (`X-Platform-Key`, the builder bootstrapping a project and its first key),
    which is no key at all."""

    API_KEY = "api_key"
    PLATFORM = "platform"


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


class EvalKind(StrEnum):
    """Which of the two kinds of scenario this is.

    The values are pipecat's own (`pipecat.evals.scenario.EvalKind`), which it
    carries into the session, the driver and the result record — so a stored
    row and a pipecat object never need translating between them.
    """

    SCRIPTED = "script"
    SIMULATION = "simulation"


class EvalModality(StrEnum):
    """Whether the conversation is spoken or typed.

    `text` skips STT and TTS entirely, so it is fast and free and covers the
    smallest share of a voice platform's risk; `audio` is the only mode that
    reaches the provider-connect path where most regressions have lived.
    """

    TEXT = "text"
    AUDIO = "audio"


class EvalRunStatus(StrEnum):
    """Where a run is, and how it ended.

    `errored` is deliberately not a kind of `failed`: it means the harness did
    not complete (a connect failure, a judge outage), which is neither a pass
    nor a fail and never counts toward a rate. A judge outage reading as an
    agent regression is how a suite loses its audience.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in (EvalRunStatus.QUEUED, EvalRunStatus.RUNNING)


class EvalModelProvider(StrEnum):
    """Who runs an eval's judge, or its persona (#118).

    A closed set on purpose. Pipecat reaches anything but Ollama through
    `factory`, a dotted path it imports — so the provider name is what a
    request carries, and `evals.judges.PROVIDERS` maps it to a callable
    TurnCall ships. The set is the allowlist.

    `ollama` stays the default: it is pipecat's, it is local, and a judge that
    needs no key is what keeps an eval runnable on a laptop.

    `jev` is judge-only, and structurally so: it is a classifier rather than an
    LLM service, so it lives in `JUDGE_PROVIDERS` and not in `PROVIDERS`, and a
    `simulator:` naming it fails the unknown-provider check with the role in the
    message (#141).
    """

    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    JEV = "jev"


class EvalToolPolicy(StrEnum):
    """What happens when the agent calls a tool with no mock (slice #71).

    `mock_only` is the default and fails closed: a scenario pointed at an agent
    whose tools have real side effects must not book a real appointment on
    every iteration. `live` is the explicit opt-in for read-only lookups.
    """

    MOCK_ONLY = "mock_only"
    LIVE = "live"

    @classmethod
    def resolve(cls, value: object) -> "EvalToolPolicy":
        """The policy to apply to a value that may be absent, or stored.

        One place decides the default, because deciding it twice is how the
        two halves drift apart and the safe one stops being the default. Fails
        closed by construction: `live` is the typed opt-in, so anything that is
        not exactly it — missing, empty, a value a future version wrote — is
        `mock_only`, and nothing here can raise on a stored row.
        """
        return cls.LIVE if str(value) == cls.LIVE.value else cls.MOCK_ONLY


class CampaignStatus(StrEnum):
    """Where a campaign is (#229, #232). `queued` until the worker first
    reaches it, `completed` once no target can be dialled again.

    `paused` dials nothing new but still settles the calls it has live;
    `cancelled` is final, like `completed`, and settles its live calls too.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CampaignTargetStatus(StrEnum):
    """One target's place in its campaign (#229, #232).

    `pending` waits to be dialled; `dialling` is claimed by a worker that has
    not asked the carrier yet; `placing` is the carrier being asked; `calling`
    has a call placed; the rest are final.

    `dialling` and `placing` are separate committed steps because a worker can
    die in either, and they mean opposite things: a dead claim in `dialling`
    never reached the carrier and goes back to `pending`, while one in
    `placing` may have rung a phone with no call row to show for it, so the
    janitor ends it `failed` rather than ever dialling it a second time.
    """

    PENDING = "pending"
    DIALLING = "dialling"
    PLACING = "placing"
    CALLING = "calling"
    COMPLETED = "completed"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in (
            CampaignTargetStatus.COMPLETED,
            CampaignTargetStatus.SUPPRESSED,
            CampaignTargetStatus.FAILED,
            CampaignTargetStatus.CANCELLED,
        )
