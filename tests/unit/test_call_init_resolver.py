"""Tests for call_init_resolver service."""

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from loguru import logger

from tests.agent_table import agent_row, agent_table
from turncall.services.agent_acceptance import AcceptedAgent, AgentRefusal
from turncall.services.call_init_resolver import (
    CallInitResult,
    accept_call_init_agent,
    resolve_call_init,
)
from turncall.storage.repositories import agent_repo

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


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, Any]:
    return agent_table(monkeypatch)


@pytest.mark.unit
class TestTheAgentIsTheProjects:
    """An agent id is not a capability (#475): call-init resolves it only in
    the project whose number (or WebRTC request) asked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["agent_id", "assistant_id"])
    async def test_another_projects_agent_resolves_to_none(self, agents, key) -> None:
        foreign = agent_row(uuid4(), {"system_prompt": "SECRET"})
        agents[foreign.id] = foreign

        result = await resolve_call_init(
            AsyncMock(), {key: str(foreign.id)}, project_id=PROJECT
        )

        assert result.agent is None
        assert result.dynamic_config is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["agent_id", "assistant_id"])
    async def test_the_projects_own_agent_resolves(self, agents, key) -> None:
        own = agent_row(PROJECT)
        agents[own.id] = own

        result = await resolve_call_init(
            AsyncMock(), {key: str(own.id)}, project_id=PROJECT
        )

        assert result.agent is own

    @pytest.mark.asyncio
    async def test_a_refused_id_is_logged_without_the_agents_config(
        self, agents, logged
    ) -> None:
        foreign = agent_row(uuid4(), {"system_prompt": "SECRET"})
        agents[foreign.id] = foreign

        await resolve_call_init(
            AsyncMock(), {"agent_id": str(foreign.id)}, project_id=PROJECT
        )

        [line] = logged
        assert str(PROJECT) in line
        assert str(foreign.id) in line
        assert "SECRET" not in line


# --- accepting the agent a call-init answer names (#476) --------------------------


@pytest.fixture
def logged():
    """Every WARNING line, message and extras both."""
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


async def _accept_answer(answer: dict[str, Any], **context: str):
    session = AsyncMock()
    result = await resolve_call_init(session, answer, project_id=PROJECT)
    return await accept_call_init_agent(session, result, project_id=PROJECT, **context)


@pytest.mark.unit
class TestAcceptCallInitAgent:
    @pytest.mark.asyncio
    async def test_an_answer_naming_no_agent_is_none(self, logged) -> None:
        assert await _accept_answer({"variables": {"name": "Ada"}}) is None
        assert logged == []

    @pytest.mark.asyncio
    async def test_an_inline_agent_is_accepted(self, logged) -> None:
        outcome = await _accept_answer({"agent": {"system_prompt": "Hi"}})

        assert isinstance(outcome, AcceptedAgent)
        assert outcome.agent_id is None
        assert logged == []

    @pytest.mark.asyncio
    async def test_a_refusal_is_logged_with_origin_project_and_reason(
        self, logged
    ) -> None:
        outcome = await _accept_answer(
            {"agent": {"llm": {"provider": "xai", "base_url": "https://gw"}}},
            call_id="c-1",
        )

        assert isinstance(outcome, AgentRefusal)
        [line] = logged
        assert "origin=call_init" in line
        assert f"project_id={PROJECT}" in line
        assert "base_url is not supported for the xai provider" in line
        assert "c-1" in line

    @pytest.mark.asyncio
    async def test_an_unknown_field_is_logged_once_naming_it(self, logged) -> None:
        outcome = await _accept_answer(
            {"agent": {"system_prompt": "Hi", "voice_style": "warm"}}
        )

        assert isinstance(outcome, AcceptedAgent)
        [line] = logged
        assert "voice_style" in line

    @pytest.mark.asyncio
    async def test_the_s2s_warnings_are_left_to_the_build_to_log(self, logged) -> None:
        """The build logs `skills_skipped` / `procedures_skipped` itself; a
        second line here would say it twice (#476)."""
        outcome = await _accept_answer(
            {"agent": {"pipeline_mode": "s2s", "skills": [{"skill": "billing"}]}}
        )

        assert isinstance(outcome, AcceptedAgent)
        assert [w.code for w in outcome.warnings] == ["skills_ignored_on_s2s"]
        assert logged == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["agent_id", "assistant_id"])
    async def test_an_agent_id_that_is_not_a_uuid_is_refused(
        self, key: str, logged
    ) -> None:
        outcome = await _accept_answer({key: "not-a-uuid"})

        assert isinstance(outcome, AgentRefusal)
        assert outcome.field == "agent_id"

    @pytest.mark.asyncio
    async def test_an_agent_id_outside_the_project_is_refused(
        self, agents, logged
    ) -> None:
        foreign = agent_row(uuid4(), {"system_prompt": "Theirs"})
        agents[foreign.id] = foreign

        outcome = await _accept_answer({"agent_id": str(foreign.id)})

        assert isinstance(outcome, AgentRefusal)
        assert outcome.field == "agent_id"
        assert "Theirs" not in outcome.reason

    @pytest.mark.asyncio
    async def test_the_projects_agent_is_accepted_from_the_row_read_once(
        self, agents, monkeypatch
    ) -> None:
        """One lookup serves the resolver and acceptance (#474, T5)."""
        own = agent_row(PROJECT, {"system_prompt": "Ours"})
        agents[own.id] = own
        reads: list[UUID] = []
        table_read = agent_repo.get_agent_by_id

        async def counted(session: Any, agent_id: UUID, **kwargs: Any) -> Any:
            reads.append(agent_id)
            return await table_read(session, agent_id, **kwargs)

        monkeypatch.setattr(agent_repo, "get_agent_by_id", counted)

        outcome = await _accept_answer({"agent_id": str(own.id)})

        assert isinstance(outcome, AcceptedAgent)
        assert outcome.agent_id == own.id
        assert outcome.config.system_prompt == "Ours"
        assert reads == [own.id]
