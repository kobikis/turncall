"""Tests for SMS session state machine."""

from datetime import UTC, datetime, timedelta

import pytest

from turncall.domain.enums import SmsSessionStatus
from turncall.domain.session_state import (
    SESSION_TTL_HOURS,
    is_session_expired,
    is_terminal_state,
    is_valid_transition,
)


@pytest.mark.unit
class TestSessionStateTransitions:
    def test_active_to_expired(self) -> None:
        assert is_valid_transition(SmsSessionStatus.ACTIVE, SmsSessionStatus.EXPIRED)

    def test_expired_cannot_transition(self) -> None:
        assert not is_valid_transition(
            SmsSessionStatus.EXPIRED, SmsSessionStatus.ACTIVE
        )

    def test_active_to_active_invalid(self) -> None:
        assert not is_valid_transition(SmsSessionStatus.ACTIVE, SmsSessionStatus.ACTIVE)


@pytest.mark.unit
class TestSessionStateHelpers:
    def test_expired_is_terminal(self) -> None:
        assert is_terminal_state(SmsSessionStatus.EXPIRED)

    def test_active_is_not_terminal(self) -> None:
        assert not is_terminal_state(SmsSessionStatus.ACTIVE)

    def test_ttl_is_24_hours(self) -> None:
        assert SESSION_TTL_HOURS == 24


@pytest.mark.unit
class TestSessionExpiry:
    def test_not_expired_when_future(self) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=12)
        assert not is_session_expired(expires_at)

    def test_expired_when_past(self) -> None:
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        assert is_session_expired(expires_at)

    def test_expired_when_exactly_now(self) -> None:
        # Edge case: expires_at == now should be expired
        expires_at = datetime.now(UTC)
        assert is_session_expired(expires_at)

    def test_not_expired_just_before(self) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=1)
        assert not is_session_expired(expires_at)
