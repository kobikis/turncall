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
