"""Call state machine - valid transitions between call states."""

from turncall.domain.enums import CallStatus, EndedReason

# Raw event_type strings used as termination signals (not all are in CallEventType)
_VOICEMAIL_EVENT = "voicemail.detected"
_TRANSFER_EVENT = "call.transferred"
_TELEPHONY_FAIL_EVENT = "call.failed"

# Defines which states can transition to which other states
VALID_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.INITIATED: frozenset(
        {
            CallStatus.RINGING,
            CallStatus.IN_PROGRESS,
            CallStatus.FAILED,
            CallStatus.NO_ANSWER,
            CallStatus.BUSY,
        }
    ),
    CallStatus.RINGING: frozenset(
        {
            CallStatus.IN_PROGRESS,
            CallStatus.FAILED,
            CallStatus.NO_ANSWER,
            CallStatus.BUSY,
            CallStatus.VOICEMAIL,
            CallStatus.COMPLETED,
        }
    ),
    CallStatus.IN_PROGRESS: frozenset(
        {
            CallStatus.TRANSFERRING,
            CallStatus.HANDED_OFF,
            CallStatus.COMPLETED,
            CallStatus.FAILED,
        }
    ),
    CallStatus.TRANSFERRING: frozenset(
        {
            CallStatus.IN_PROGRESS,  # transfer failed, back to call
            CallStatus.COMPLETED,
            CallStatus.FAILED,
        }
    ),
    CallStatus.HANDED_OFF: frozenset(
        {
            CallStatus.IN_PROGRESS,  # new agent takes over
            CallStatus.COMPLETED,
            CallStatus.FAILED,
        }
    ),
    # Terminal states - no transitions out
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
    CallStatus.NO_ANSWER: frozenset(),
    CallStatus.BUSY: frozenset(),
    CallStatus.VOICEMAIL: frozenset(
        {
            CallStatus.COMPLETED,
        }
    ),
}


def is_valid_transition(current: CallStatus, target: CallStatus) -> bool:
    """Check if a state transition is allowed."""
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    return target in allowed


def is_terminal_state(status: CallStatus) -> bool:
    """Check if a call status is terminal (no further transitions)."""
    return status in {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
    }


def is_active_state(status: CallStatus) -> bool:
    """Check if a call is in an active (non-terminal) state."""
    return status in {
        CallStatus.INITIATED,
        CallStatus.RINGING,
        CallStatus.IN_PROGRESS,
        CallStatus.TRANSFERRING,
        CallStatus.HANDED_OFF,
        CallStatus.VOICEMAIL,
    }


# Map Twilio call status strings to internal CallStatus
TWILIO_STATUS_MAP: dict[str, CallStatus] = {
    "queued": CallStatus.INITIATED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "failed": CallStatus.FAILED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.FAILED,
}


def infer_ended_reason(
    status: str,
    event_types: set[str],
    *,
    assistant_ended: bool,
) -> EndedReason:
    """Derive the granular reason a call ended. Pure; see ADR-0008.

    Args:
        status: the call's terminal CallStatus value.
        event_types: distinct event_type strings recorded for the call.
        assistant_ended: a `call.ended` event with payload source == "control"
            exists (the end_call tool fired).

    Precedence is fixed, first match wins.
    """
    if _VOICEMAIL_EVENT in event_types:
        return EndedReason.VOICEMAIL
    if _TRANSFER_EVENT in event_types:
        return EndedReason.TRANSFERRED
    if assistant_ended:
        return EndedReason.ASSISTANT_ENDED_CALL
    if status == CallStatus.NO_ANSWER:
        return EndedReason.CUSTOMER_DID_NOT_ANSWER
    if status == CallStatus.BUSY:
        return EndedReason.CUSTOMER_BUSY
    if status == CallStatus.FAILED:
        return (
            EndedReason.TELEPHONY_FAILED
            if _TELEPHONY_FAIL_EVENT in event_types
            else EndedReason.PIPELINE_ERROR
        )
    if status == CallStatus.COMPLETED:
        return EndedReason.CUSTOMER_ENDED_CALL
    return EndedReason.UNKNOWN
