"""Tests for call_init_resolver service."""

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from loguru import logger

from turncall.services.call_init_resolver import (
    CallInitResult,
    resolve_call_init,
)

PROJECT = uuid4()


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

            result = await resolve_call_init(
                session, {"agent_id": agent_id}, project_id=PROJECT
            )

        assert result.agent == fake_assistant
        assert result.dynamic_config is None
        mock_repo.get_agent_by_id.assert_called_once_with(
            session, UUID(agent_id), project_id=PROJECT
        )

    @pytest.mark.asyncio
    async def test_resolves_inline_config(self) -> None:
        """When response contains assistant dict, returns as dynamic_config."""
        inline_config = {
            "system_prompt": "You are a bot",
            "llm": {"provider": "openai"},
        }
        session = AsyncMock()

        result = await resolve_call_init(
            session, {"assistant": inline_config}, project_id=PROJECT
        )

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
                project_id=PROJECT,
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
            project_id=PROJECT,
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
            project_id=PROJECT,
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
            project_id=PROJECT,
        )

        assert result.knowledge_context is None

    @pytest.mark.asyncio
    async def test_backward_compat_no_new_fields(self) -> None:
        """Response without metadata/dynamic_data still works."""
        session = AsyncMock()

        result = await resolve_call_init(
            session,
            {"assistant": {"system_prompt": "hi"}},
            project_id=PROJECT,
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
            project_id=PROJECT,
        )

        assert result.agent is None
        assert result.dynamic_config is None

    @pytest.mark.asyncio
    async def test_result_is_frozen(self) -> None:
        """CallInitResult is immutable."""
        result = CallInitResult()
        with pytest.raises(Exception):
            result.knowledge_context = "mutate"  # type: ignore[misc]


def _agents_of(owner: UUID, agent: Any):
    """`agent_repo.get_agent_by_id` over one agent owned by `owner`: found only
    when the lookup is scoped to that project."""

    async def _get(_session: Any, agent_id: UUID, *, project_id: Any = None) -> Any:
        if agent_id == agent.id and project_id == owner:
            return agent
        return None

    return _get


@pytest.mark.unit
class TestTheAgentIsTheProjects:
    """An agent id is not a capability (#475): call-init resolves it only in
    the project whose number (or WebRTC request) asked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["agent_id", "assistant_id"])
    async def test_another_projects_agent_resolves_to_none(self, key: str) -> None:
        foreign = AsyncMock(id=uuid4(), config_blob={"system_prompt": "SECRET"})
        with patch("turncall.services.call_init_resolver.agent_repo") as repo:
            repo.get_agent_by_id = _agents_of(uuid4(), foreign)
            result = await resolve_call_init(
                AsyncMock(), {key: str(foreign.id)}, project_id=PROJECT
            )

        assert result.agent is None
        assert result.dynamic_config is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["agent_id", "assistant_id"])
    async def test_the_projects_own_agent_resolves(self, key: str) -> None:
        own = AsyncMock(id=uuid4())
        with patch("turncall.services.call_init_resolver.agent_repo") as repo:
            repo.get_agent_by_id = _agents_of(PROJECT, own)
            result = await resolve_call_init(
                AsyncMock(), {key: str(own.id)}, project_id=PROJECT
            )

        assert result.agent is own

    @pytest.mark.asyncio
    async def test_a_refused_id_is_logged_without_the_agents_config(self) -> None:
        foreign = AsyncMock(id=uuid4(), config_blob={"system_prompt": "SECRET"})
        lines: list[str] = []
        sink = logger.add(
            lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
        )
        try:
            with patch("turncall.services.call_init_resolver.agent_repo") as repo:
                repo.get_agent_by_id = _agents_of(uuid4(), foreign)
                await resolve_call_init(
                    AsyncMock(), {"agent_id": str(foreign.id)}, project_id=PROJECT
                )
        finally:
            logger.remove(sink)

        [line] = lines
        assert str(PROJECT) in line
        assert str(foreign.id) in line
        assert "SECRET" not in line
