"""Tests for call control service (ControlResult model)."""

import pytest

from turncall.services.call_control import ControlResult


@pytest.mark.unit
class TestControlResult:
    def test_success_result(self) -> None:
        result = ControlResult(success=True, message="Call ended")
        assert result.success is True
        assert result.message == "Call ended"
        assert result.details is None

    def test_failure_result(self) -> None:
        result = ControlResult(
            success=False,
            message="Call not found",
            details={"call_id": "abc"},
        )
        assert result.success is False
        assert result.details is not None

    def test_result_is_frozen(self) -> None:
        result = ControlResult(success=True, message="ok")
        with pytest.raises(Exception):
            result.success = False  # type: ignore[misc]
