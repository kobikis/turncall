"""Tests for domain models (immutability, serialization)."""

import uuid
from datetime import UTC, datetime

import pytest

from turncall.domain.enums import AgentState, CallDirection, CallStatus
from turncall.domain.models import (
    Agent,
    AgentConfig,
    Call,
    Project,
    STTConfig,
)


@pytest.mark.unit
class TestDomainModelImmutability:
    def test_project_is_frozen(self) -> None:
        project = Project(
            id=uuid.uuid4(),
            name="test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            project.name = "mutated"  # type: ignore[misc]

    def test_call_is_frozen(self) -> None:
        call = Call(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            direction=CallDirection.INBOUND,
            status=CallStatus.INITIATED,
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            call.status = CallStatus.COMPLETED  # type: ignore[misc]

    def test_agent_is_frozen(self) -> None:
        agent = Agent(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="test-agent",
            environment="development",
            version=1,
            state=AgentState.DRAFT,
            config=AgentConfig(),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(Exception):
            agent.name = "mutated"  # type: ignore[misc]


@pytest.mark.unit
class TestDomainModelSerialization:
    def test_assistant_config_defaults(self) -> None:
        config = AgentConfig()
        assert config.stt.provider == "deepgram"
        assert config.llm.model == "gpt-4o-mini"
        assert config.tts.provider == "deepgram"
        assert config.silence_timeout_ms == 800
        assert config.interruption_enabled is True

    def test_stt_config_custom(self) -> None:
        config = STTConfig(provider="openai", model="whisper-1", language="en")
        data = config.model_dump()
        assert data["provider"] == "openai"
        assert data["language"] == "en"

    def test_call_serialization_roundtrip(self) -> None:
        call_id = uuid.uuid4()
        project_id = uuid.uuid4()
        now = datetime.now(UTC)

        call = Call(
            id=call_id,
            project_id=project_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            from_number="+15551234567",
            to_number="+15559876543",
            created_at=now,
        )
        data = call.model_dump()
        restored = Call.model_validate(data)

        assert restored.id == call_id
        assert restored.direction == CallDirection.OUTBOUND
        assert restored.from_number == "+15551234567"
