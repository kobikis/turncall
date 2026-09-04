"""infer_ended_reason — derived call-end reason. See ADR-0008.

Covers each precedence branch and the fragile failed-split.
"""

import pytest

from turncall.domain.call_state import infer_ended_reason
from turncall.domain.enums import EndedReason

pytestmark = pytest.mark.unit


def test_voicemail_wins_over_completed():
    # Voicemail call that then completes still resolves to voicemail.
    reason = infer_ended_reason(
        "completed", {"voicemail.detected", "call.ended"}, assistant_ended=False
    )
    assert reason == EndedReason.VOICEMAIL


def test_transferred():
    reason = infer_ended_reason(
        "completed", {"call.transferred"}, assistant_ended=False
    )
    assert reason == EndedReason.TRANSFERRED


def test_assistant_ended_beats_completed_status():
    reason = infer_ended_reason("completed", {"call.ended"}, assistant_ended=True)
    assert reason == EndedReason.ASSISTANT_ENDED_CALL


def test_no_answer():
    reason = infer_ended_reason("no_answer", set(), assistant_ended=False)
    assert reason == EndedReason.CUSTOMER_DID_NOT_ANSWER


def test_busy():
    reason = infer_ended_reason("busy", set(), assistant_ended=False)
    assert reason == EndedReason.CUSTOMER_BUSY


def test_failed_with_telephony_event_is_telephony_failed():
    reason = infer_ended_reason("failed", {"call.failed"}, assistant_ended=False)
    assert reason == EndedReason.TELEPHONY_FAILED


def test_failed_without_telephony_event_is_pipeline_error():
    reason = infer_ended_reason("failed", set(), assistant_ended=False)
    assert reason == EndedReason.PIPELINE_ERROR


def test_completed_fallback_is_customer_ended():
    reason = infer_ended_reason("completed", {"transcript.final"}, assistant_ended=False)
    assert reason == EndedReason.CUSTOMER_ENDED_CALL


def test_unknown_status():
    reason = infer_ended_reason("in_progress", set(), assistant_ended=False)
    assert reason == EndedReason.UNKNOWN
