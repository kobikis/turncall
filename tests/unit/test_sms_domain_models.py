"""Tests for SMS domain models (immutability, serialization)."""

import uuid
from datetime import UTC, datetime

import pytest

from turncall.domain.enums import ChatChannel, SmsMessageRole, SmsSessionStatus
from turncall.domain.models import SmsMessage, SmsSession


@pytest.mark.unit
class TestSmsSessionImmutability:
    def test_session_is_frozen(self) -> None:
        session = SmsSession(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            customer_number="+15551234567",
            turncall_number="+15559876543",
            status=SmsSessionStatus.ACTIVE,
            channel=ChatChannel.SMS,
            last_activity_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            session.status = SmsSessionStatus.EXPIRED  # type: ignore[misc]

    def test_message_is_frozen(self) -> None:
        message = SmsMessage(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            role=SmsMessageRole.CUSTOMER,
            content="Hello",
            channel=ChatChannel.SMS,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            message.content = "mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestSmsSessionSerialization:
    def test_session_roundtrip(self) -> None:
        session_id = uuid.uuid4()
        now = datetime.now(UTC)
        session = SmsSession(
            id=session_id,
            project_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            customer_number="+15551234567",
            turncall_number="+15559876543",
            status=SmsSessionStatus.ACTIVE,
            channel=ChatChannel.SMS,
            message_count=5,
            last_activity_at=now,
            expires_at=now,
            created_at=now,
        )
        data = session.model_dump()
        restored = SmsSession.model_validate(data)
        assert restored.id == session_id
        assert restored.status == SmsSessionStatus.ACTIVE
        assert restored.message_count == 5

    def test_message_roundtrip(self) -> None:
        msg_id = uuid.uuid4()
        now = datetime.now(UTC)
        message = SmsMessage(
            id=msg_id,
            session_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            role=SmsMessageRole.ASSISTANT,
            content="Hello, how can I help?",
            channel=ChatChannel.API,
            token_count=42,
            created_at=now,
        )
        data = message.model_dump()
        restored = SmsMessage.model_validate(data)
        assert restored.id == msg_id
        assert restored.role == SmsMessageRole.ASSISTANT
        assert restored.token_count == 42
        assert restored.channel == ChatChannel.API


@pytest.mark.unit
class TestSmsEnums:
    def test_session_status_values(self) -> None:
        assert SmsSessionStatus.ACTIVE == "active"
        assert SmsSessionStatus.EXPIRED == "expired"

    def test_message_role_values(self) -> None:
        assert SmsMessageRole.CUSTOMER == "customer"
        assert SmsMessageRole.ASSISTANT == "assistant"
        assert SmsMessageRole.SYSTEM == "system"

    def test_chat_channel_values(self) -> None:
        assert ChatChannel.SMS == "sms"
        assert ChatChannel.WEB == "web"
        assert ChatChannel.API == "api"
