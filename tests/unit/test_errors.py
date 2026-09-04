"""Tests for error handling framework."""

import pytest

from turncall.api.errors import (
    ConflictError,
    ErrorCode,
    ErrorResponse,
    ForbiddenError,
    InvalidStateTransitionError,
    NotFoundError,
    UnauthorizedError,
)


@pytest.mark.unit
class TestApiErrors:
    def test_not_found_error(self) -> None:
        err = NotFoundError("Assistant", "abc-123")
        assert err.status_code == 404
        assert err.code == ErrorCode.NOT_FOUND
        assert "abc-123" in err.message

    def test_conflict_error(self) -> None:
        err = ConflictError("Already exists")
        assert err.status_code == 409
        assert err.code == ErrorCode.CONFLICT

    def test_unauthorized_error(self) -> None:
        err = UnauthorizedError()
        assert err.status_code == 401

    def test_forbidden_error(self) -> None:
        err = ForbiddenError()
        assert err.status_code == 403

    def test_invalid_state_transition(self) -> None:
        err = InvalidStateTransitionError("initiated", "completed")
        assert err.status_code == 409
        assert "initiated" in err.message
        assert "completed" in err.message
        assert err.details is not None
        assert err.details["current_state"] == "initiated"

    def test_error_response_is_frozen(self) -> None:
        resp = ErrorResponse(
            error="test",
            code=ErrorCode.INTERNAL_ERROR,
        )
        with pytest.raises(Exception):
            resp.error = "mutated"  # type: ignore[misc]
