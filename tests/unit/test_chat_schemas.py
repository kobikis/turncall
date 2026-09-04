"""Tests for chat API schemas."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.chat import (
    ChatMessageResponse,
    ChatSessionResponse,
    SendChatMessageRequest,
)


@pytest.mark.unit
class TestSendChatMessageRequest:
    def test_minimal_valid(self) -> None:
        req = SendChatMessageRequest(
            agent_id=uuid.uuid4(),
            message="Hello",
        )
        assert req.channel == "api"
        assert req.session_id is None
        assert req.previous_chat_id is None

    def test_with_session_id(self) -> None:
        sid = uuid.uuid4()
        req = SendChatMessageRequest(
            agent_id=uuid.uuid4(),
            message="Hello",
            session_id=sid,
        )
        assert req.session_id == sid

    def test_with_previous_chat_id(self) -> None:
        prev_id = uuid.uuid4()
        req = SendChatMessageRequest(
            agent_id=uuid.uuid4(),
            message="Hello",
            previous_chat_id=prev_id,
        )
        assert req.previous_chat_id == prev_id

    def test_cannot_have_both_session_and_previous(self) -> None:
        with pytest.raises(ValidationError, match="Cannot provide both"):
            SendChatMessageRequest(
                agent_id=uuid.uuid4(),
                message="Hello",
                session_id=uuid.uuid4(),
                previous_chat_id=uuid.uuid4(),
            )

    def test_empty_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendChatMessageRequest(
                agent_id=uuid.uuid4(),
                message="",
            )

    def test_message_max_length(self) -> None:
        with pytest.raises(ValidationError):
            SendChatMessageRequest(
                agent_id=uuid.uuid4(),
                message="x" * 4097,
            )

    def test_invalid_channel_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendChatMessageRequest(
                agent_id=uuid.uuid4(),
                message="Hello",
                channel="invalid",
            )

    def test_valid_channels(self) -> None:
        for channel in ("sms", "web", "api"):
            req = SendChatMessageRequest(
                agent_id=uuid.uuid4(),
                message="Hello",
                channel=channel,
            )
            assert req.channel == channel


@pytest.mark.unit
class TestChatMessageResponse:
    def test_frozen(self) -> None:
        resp = ChatMessageResponse(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            role="assistant",
            content="Hi",
            channel="sms",
            token_count=10,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            resp.content = "mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestChatSessionResponse:
    def test_frozen(self) -> None:
        resp = ChatSessionResponse(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            customer_number="+15551234567",
            turncall_number="+15559876543",
            status="active",
            channel="sms",
            message_count=3,
            last_activity_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            resp.status = "expired"  # type: ignore[misc]
