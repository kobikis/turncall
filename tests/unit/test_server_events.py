"""Tests for Vapi-style server events."""

import pytest

from turncall.events.server_events import (
    ServerEventRequest,
    ServerEventResponse,
    ServerEventType,
)


@pytest.mark.unit
class TestServerEventTypes:
    def test_all_event_types(self) -> None:
        expected = {
            "call-init",
            "function-call",
            "call-end",
            "status-update",
            "speech-update",
            "transcript-update",
            "hang",
        }
        actual = {e.value for e in ServerEventType}
        assert actual == expected


@pytest.mark.unit
class TestServerEventRequest:
    def test_create_request(self) -> None:
        req = ServerEventRequest(
            event_type=ServerEventType.CALL_INIT,
            call_id="call-123",
            payload={"phoneNumber": {"number": "+15551234567"}},
        )
        assert req.event_type == ServerEventType.CALL_INIT
        assert req.call_id == "call-123"

    def test_function_call_request(self) -> None:
        req = ServerEventRequest(
            event_type=ServerEventType.FUNCTION_CALL,
            call_id="call-456",
            payload={
                "functionCall": {
                    "name": "lookup_customer",
                    "parameters": {"id": "cust-789"},
                },
            },
        )
        assert req.payload["functionCall"]["name"] == "lookup_customer"


@pytest.mark.unit
class TestServerEventResponse:
    def test_success_response(self) -> None:
        resp = ServerEventResponse(
            success=True,
            status_code=200,
            data={"agent_id": "asst-123"},
        )
        assert resp.success is True
        assert resp.data is not None

    def test_error_response(self) -> None:
        resp = ServerEventResponse(
            success=False,
            status_code=0,
            error="Connection timeout",
        )
        assert resp.success is False
        assert resp.error is not None

    def test_response_with_inline_assistant(self) -> None:
        resp = ServerEventResponse(
            success=True,
            status_code=200,
            data={
                "assistant": {
                    "system_prompt": "You are a support agent",
                    "first_message": "Hello!",
                },
            },
        )
        assert "assistant" in resp.data  # type: ignore[operator]


@pytest.mark.unit
class TestPhoneNumberWebhookRouting:
    def test_bind_request_webhook_requires_server_url(self) -> None:
        from pydantic import ValidationError

        from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest

        with pytest.raises(ValidationError, match="server_url is required"):
            BindPhoneNumberRequest(
                external_number_sid="PN123",
                e164_number="+15551234567",
                routing_target_type="webhook",
            )

    def test_bind_request_webhook_with_server_url(self) -> None:
        from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest

        req = BindPhoneNumberRequest(
            external_number_sid="PN123",
            e164_number="+15551234567",
            routing_target_type="webhook",
            server_url="https://my-server.com/turncall-events",
        )
        assert req.server_url is not None
        assert req.routing_target_id is None

    def test_bind_request_assistant_requires_target_id(self) -> None:
        from pydantic import ValidationError

        from turncall.api.v1.schemas.phone_numbers import BindPhoneNumberRequest

        with pytest.raises(ValidationError, match="routing_target_id is required"):
            BindPhoneNumberRequest(
                external_number_sid="PN123",
                e164_number="+15551234567",
                routing_target_type="agent",
            )
