"""SMS session state machine - valid transitions and expiry logic."""

from datetime import UTC, datetime

from turncall.domain.enums import SmsSessionStatus

SESSION_TTL_HOURS: int = 24

VALID_TRANSITIONS: dict[SmsSessionStatus, frozenset[SmsSessionStatus]] = {
    SmsSessionStatus.ACTIVE: frozenset({SmsSessionStatus.EXPIRED}),
    # Terminal state - no transitions out
    SmsSessionStatus.EXPIRED: frozenset(),
}


def is_valid_transition(current: SmsSessionStatus, target: SmsSessionStatus) -> bool:
    """Check if a session state transition is allowed."""
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    return target in allowed


def is_terminal_state(status: SmsSessionStatus) -> bool:
    """Check if a session status is terminal."""
    return status == SmsSessionStatus.EXPIRED


def is_session_expired(expires_at: datetime) -> bool:
    """Check if a session has expired based on its expiry timestamp."""
    now = datetime.now(UTC)
    return now >= expires_at


# Redaction of a text conversation (#314), kept in `metadata_json` under
# REDACTION: on a session, PENDING from its expiry while its project redacts
# until its rows are rewritten, then DONE; on a message, DONE once its content
# is. A row without it holds what was said, raw. CLAIMED_UNTIL: a pending
# session a sweep is rewriting, until when (a lease, so a replica that dies
# mid-rewrite does not keep it).
REDACTION = "redaction"
PENDING = "pending"
DONE = "done"
CLAIMED_UNTIL = "redaction_claimed_until"
