"""Tests for phone number schemas with sms_enabled field."""

import uuid

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.phone_numbers import (
    BindPhoneNumberRequest,
    UpdatePhoneNumberRequest,
)


@pytest.mark.unit
class TestBindPhoneNumberRequest:
    def test_sms_enabled_default_false(self) -> None:
        req = BindPhoneNumberRequest(
            external_number_sid="PN123",
            e164_number="+15551234567",
            routing_target_type="agent",
            routing_target_id=uuid.uuid4(),
        )
        assert req.sms_enabled is False

    def test_sms_enabled_true(self) -> None:
        req = BindPhoneNumberRequest(
            external_number_sid="PN123",
            e164_number="+15551234567",
            routing_target_type="agent",
            routing_target_id=uuid.uuid4(),
            sms_enabled=True,
        )
        assert req.sms_enabled is True

    def test_webhook_requires_server_url(self) -> None:
        with pytest.raises(ValidationError, match="server_url is required"):
            BindPhoneNumberRequest(
                external_number_sid="PN123",
                e164_number="+15551234567",
                routing_target_type="webhook",
            )

    def test_assistant_requires_routing_target_id(self) -> None:
        with pytest.raises(ValidationError, match="routing_target_id is required"):
            BindPhoneNumberRequest(
                external_number_sid="PN123",
                e164_number="+15551234567",
                routing_target_type="agent",
            )


@pytest.mark.unit
class TestUpdatePhoneNumberRequest:
    def test_agent_routing(self) -> None:
        req = UpdatePhoneNumberRequest(
            routing_target_type="agent", routing_target_id=uuid.uuid4()
        )
        assert req.server_url is None
        assert req.sms_enabled is False

    def test_webhook_requires_server_url(self) -> None:
        with pytest.raises(ValidationError, match="server_url is required"):
            UpdatePhoneNumberRequest(routing_target_type="webhook")

    def test_agent_requires_routing_target_id(self) -> None:
        with pytest.raises(ValidationError, match="routing_target_id is required"):
            UpdatePhoneNumberRequest(routing_target_type="agent")
