"""Tests for webhook delivery data structures."""

import uuid

import pytest

from turncall.events.webhook_delivery import DeliveryResult, WebhookEvent


@pytest.mark.unit
class TestWebhookEvent:
    def test_create_event(self) -> None:
        event = WebhookEvent(
            event_type="call.ended",
            payload={"duration": 120},
            project_id=uuid.uuid4(),
            call_id=uuid.uuid4(),
        )
        assert event.event_type == "call.ended"
        assert event.payload["duration"] == 120

    def test_event_without_call_id(self) -> None:
        event = WebhookEvent(
            event_type="project.updated",
            payload={},
            project_id=uuid.uuid4(),
        )
        assert event.call_id is None


@pytest.mark.unit
class TestDeliveryResult:
    def test_success_result(self) -> None:
        result = DeliveryResult(success=True, status_code=200, attempts=1)
        assert result.success is True

    def test_failure_result(self) -> None:
        result = DeliveryResult(success=False, attempts=5, error="Max retries exceeded")
        assert not result.success
        assert result.error is not None
