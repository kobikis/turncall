"""Tests for call_init_resolver service."""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from turncall.services.call_init_resolver import (
    CallInitResult,
    resolve_call_init,
)


@pytest.mark.unit
class TestResolveCallInit:
    """Test the shared call-init response resolver."""

    @pytest.mark.asyncio
    async def test_resolves_assistant_by_id(self) -> None:
        """When response contains agent_id, looks it up in DB."""
        fake_assistant = AsyncMock()
        fake_assistant.id = uuid4()
        agent_id = str(fake_assistant.id)

        session = AsyncMock()

        with patch("turncall.services.call_init_resolver.agent_repo") as mock_repo:
            mock_repo.get_agent_by_id = AsyncMock(return_value=fake_assistant)

            result = await resolve_call_init(session, {"agent_id": agent_id})

        assert result.agent == fake_assistant
        assert result.dynamic_config is None
        mock_repo.get_agent_by_id.assert_called_once_with(
            session, UUID(agent_id)
        )

    @pytest.mark.asyncio
    async def test_resolves_inline_config(self) -> None:
        """When response contains assistant dict, returns as dynamic_config."""
        inline_config = {
            "system_prompt": "You are a bot",
            "llm": {"provider": "openai"},
        }
        session = AsyncMock()

        result = await resolve_call_init(session, {"assistant": inline_config})

        assert result.agent is None
        assert result.dynamic_config == inline_config

    @pytest.mark.asyncio
    async def test_extracts_template_variables(self) -> None:
        """Variables dict is extracted and stringified."""
        session = AsyncMock()

        with patch("turncall.services.call_init_resolver.agent_repo") as mock_repo:
            mock_repo.get_agent_by_id = AsyncMock(return_value=AsyncMock())

            result = await resolve_call_init(
                session,
                {
                    "agent_id": str(uuid4()),
                    "variables": {"name": "Jane", "count": 42},
                },
            )

        assert result.template_variables == {"name": "Jane", "count": "42"}

    @pytest.mark.asyncio
    async def test_extracts_metadata(self) -> None:
        """Metadata dict is extracted."""
        session = AsyncMock()
        metadata = {"crm_id": "C-123", "priority": "high"}

        result = await resolve_call_init(
            session,
            {"assistant": {"system_prompt": "hi"}, "metadata": metadata},
        )

        assert result.metadata == metadata

    @pytest.mark.asyncio
    async def test_extracts_knowledge_context(self) -> None:
        """knowledge_context is extracted from dynamic_data."""
        session = AsyncMock()
        kc = "Customer has open ticket #456 about billing."

        result = await resolve_call_init(
            session,
            {
                "assistant": {"system_prompt": "hi"},
                "dynamic_data": {"knowledge_context": kc},
            },
        )

        assert result.knowledge_context == kc

    @pytest.mark.asyncio
    async def test_empty_knowledge_context_ignored(self) -> None:
        """Whitespace-only knowledge_context treated as None."""
        session = AsyncMock()

        result = await resolve_call_init(
            session,
            {
                "assistant": {"system_prompt": "hi"},
                "dynamic_data": {"knowledge_context": "   "},
            },
        )

        assert result.knowledge_context is None

    @pytest.mark.asyncio
    async def test_backward_compat_no_new_fields(self) -> None:
        """Response without metadata/dynamic_data still works."""
        session = AsyncMock()

        result = await resolve_call_init(
            session,
            {"assistant": {"system_prompt": "hi"}},
        )

        assert result.metadata == {}
        assert result.knowledge_context is None
        assert result.template_variables == {}

    @pytest.mark.asyncio
    async def test_invalid_agent_id_handled(self) -> None:
        """Invalid UUID in agent_id doesn't crash."""
        session = AsyncMock()

        result = await resolve_call_init(
            session,
            {"agent_id": "not-a-uuid"},
        )

        assert result.agent is None
        assert result.dynamic_config is None

    @pytest.mark.asyncio
    async def test_result_is_frozen(self) -> None:
        """CallInitResult is immutable."""
        result = CallInitResult()
        with pytest.raises(Exception):
            result.knowledge_context = "mutate"  # type: ignore[misc]
