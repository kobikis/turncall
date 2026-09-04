"""Tests for call control schemas."""

import uuid

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.call_control import (
    EndCallRequest,
    HandoffCallRequest,
    InjectContextRequest,
    SendDtmfRequest,
    TransferCallRequest,
)


@pytest.mark.unit
class TestTransferCallRequest:
    def test_valid_request(self) -> None:
        req = TransferCallRequest(target_number="+15551234567")
        assert req.transfer_mode == "cold"

    def test_warm_transfer(self) -> None:
        req = TransferCallRequest(
            target_number="+15551234567",
            transfer_mode="warm",
        )
        assert req.transfer_mode == "warm"

    def test_invalid_number_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TransferCallRequest(target_number="not-a-number")

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TransferCallRequest(
                target_number="+15551234567",
                transfer_mode="invalid",
            )


@pytest.mark.unit
class TestHandoffCallRequest:
    def test_valid_request(self) -> None:
        req = HandoffCallRequest(target_agent_id=uuid.uuid4())
        assert req.reason is None

    def test_with_reason_and_context(self) -> None:
        req = HandoffCallRequest(
            target_agent_id=uuid.uuid4(),
            reason="customer needs billing",
            context_payload={"account_id": "123"},
        )
        assert req.reason == "customer needs billing"
        assert req.context_payload is not None


@pytest.mark.unit
class TestSendDtmfRequest:
    def test_valid_digits(self) -> None:
        SendDtmfRequest(digits="1234567890")
        SendDtmfRequest(digits="*#ABCD")
        SendDtmfRequest(digits="1w2")

    def test_invalid_digits_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendDtmfRequest(digits="xyz")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendDtmfRequest(digits="")


@pytest.mark.unit
class TestInjectContextRequest:
    def test_valid_request(self) -> None:
        req = InjectContextRequest(message="The customer's name is John")
        assert req.role == "system"

    def test_custom_role(self) -> None:
        req = InjectContextRequest(message="hello", role="user")
        assert req.role == "user"

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InjectContextRequest(message="hello", role="invalid")

    def test_empty_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InjectContextRequest(message="")


@pytest.mark.unit
class TestEndCallRequest:
    def test_default_reason(self) -> None:
        req = EndCallRequest()
        assert req.reason == "api_request"

    def test_custom_reason(self) -> None:
        req = EndCallRequest(reason="timeout")
        assert req.reason == "timeout"
