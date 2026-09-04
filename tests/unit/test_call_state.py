"""Tests for call state machine."""

import pytest

from turncall.domain.call_state import (
    TWILIO_STATUS_MAP,
    is_active_state,
    is_terminal_state,
    is_valid_transition,
)
from turncall.domain.enums import CallStatus


@pytest.mark.unit
class TestCallStateTransitions:
    def test_initiated_to_ringing(self) -> None:
        assert is_valid_transition(CallStatus.INITIATED, CallStatus.RINGING)

    def test_initiated_to_in_progress(self) -> None:
        assert is_valid_transition(CallStatus.INITIATED, CallStatus.IN_PROGRESS)

    def test_initiated_to_failed(self) -> None:
        assert is_valid_transition(CallStatus.INITIATED, CallStatus.FAILED)

    def test_ringing_to_in_progress(self) -> None:
        assert is_valid_transition(CallStatus.RINGING, CallStatus.IN_PROGRESS)

    def test_in_progress_to_transferring(self) -> None:
        assert is_valid_transition(CallStatus.IN_PROGRESS, CallStatus.TRANSFERRING)

    def test_in_progress_to_handed_off(self) -> None:
        assert is_valid_transition(CallStatus.IN_PROGRESS, CallStatus.HANDED_OFF)

    def test_in_progress_to_completed(self) -> None:
        assert is_valid_transition(CallStatus.IN_PROGRESS, CallStatus.COMPLETED)

    def test_completed_cannot_transition(self) -> None:
        assert not is_valid_transition(CallStatus.COMPLETED, CallStatus.IN_PROGRESS)
        assert not is_valid_transition(CallStatus.COMPLETED, CallStatus.FAILED)

    def test_failed_cannot_transition(self) -> None:
        assert not is_valid_transition(CallStatus.FAILED, CallStatus.IN_PROGRESS)

    def test_invalid_transition_rejected(self) -> None:
        assert not is_valid_transition(CallStatus.INITIATED, CallStatus.COMPLETED)
        assert not is_valid_transition(CallStatus.RINGING, CallStatus.TRANSFERRING)

    def test_transferring_can_go_back_to_in_progress(self) -> None:
        assert is_valid_transition(CallStatus.TRANSFERRING, CallStatus.IN_PROGRESS)

    def test_handed_off_can_go_back_to_in_progress(self) -> None:
        assert is_valid_transition(CallStatus.HANDED_OFF, CallStatus.IN_PROGRESS)


@pytest.mark.unit
class TestCallStateHelpers:
    def test_terminal_states(self) -> None:
        assert is_terminal_state(CallStatus.COMPLETED)
        assert is_terminal_state(CallStatus.FAILED)
        assert is_terminal_state(CallStatus.NO_ANSWER)
        assert is_terminal_state(CallStatus.BUSY)

    def test_non_terminal_states(self) -> None:
        assert not is_terminal_state(CallStatus.INITIATED)
        assert not is_terminal_state(CallStatus.IN_PROGRESS)
        assert not is_terminal_state(CallStatus.TRANSFERRING)

    def test_active_states(self) -> None:
        assert is_active_state(CallStatus.INITIATED)
        assert is_active_state(CallStatus.RINGING)
        assert is_active_state(CallStatus.IN_PROGRESS)
        assert is_active_state(CallStatus.TRANSFERRING)
        assert is_active_state(CallStatus.HANDED_OFF)

    def test_inactive_states(self) -> None:
        assert not is_active_state(CallStatus.COMPLETED)
        assert not is_active_state(CallStatus.FAILED)


@pytest.mark.unit
class TestTwilioStatusMapping:
    def test_all_twilio_statuses_mapped(self) -> None:
        expected_statuses = {
            "queued",
            "ringing",
            "in-progress",
            "completed",
            "failed",
            "busy",
            "no-answer",
            "canceled",
        }
        assert set(TWILIO_STATUS_MAP.keys()) == expected_statuses

    def test_mapping_values_are_valid_call_statuses(self) -> None:
        for twilio_status, internal_status in TWILIO_STATUS_MAP.items():
            assert isinstance(
                internal_status, CallStatus
            ), f"Mapping for '{twilio_status}' is not a CallStatus"
