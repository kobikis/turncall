"""Knowledge bases on S2S agents (#254).

`create_pipeline` used to return the S2S build before it looked at the agent's
knowledge bases, so on every S2S agent all three modes were dropped with
nothing said. Now `prompt` text leads every provider's instructions, `tool`
mode's `query_knowledge` reaches whoever calls tools on that provider — the
realtime service itself, or gpt-live-1's delegate — and `auto`, which has no
per-turn point ahead of a realtime model to run at, is a warning at build and
on the agent read. Seam 1 is the pipeline build, seam 2 the assembly that
loads the attachments, seam 3 the agent read.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import create_pipeline
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit

_EMPTY = {"type": "object", "properties": {}}
_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": _EMPTY,
    "webhook_url": "https://example.com/book",
}
_AWS = {"access_key_id": "AKIA", "secret_access_key": "s", "region": "us-east-1"}
PREAMBLE = "We open at nine and close at five."


def _attachment(mode: str, **over: Any) -> dict[str, Any]:
    return {
        "knowledge_base_id": uuid4(),
        "mode": mode,
        "top_k": 3,
        "similarity_threshold": 0.3,
        "tool_description": None,
        **over,
    }


def _config(provider: str, **config: Any) -> AgentConfig:
    blob: dict[str, Any] = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are a receptionist.",
        "s2s": {"provider": provider, **config.pop("s2s", {})},
        **config,
    }
    if provider == "aws":
        blob["aws"] = _AWS
    return AgentConfig.model_validate(blob)


def _build(
    config: AgentConfig,
    attachments: list[dict[str, Any]] | None = None,
    preamble: str = "",
) -> tuple[Any, Any]:
    """Build the real S2S pipeline; return its LLM service and context."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.llm_service import LLMService

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True, records=False, has_database=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", google="g", anthropic="sk-ant"),
        knowledge_base_attachments=attachments,
        knowledge_preamble=preamble,
    )
    service = next(p for p in pipeline.processors if isinstance(p, LLMService))
    user = next(p for p in pipeline.processors if isinstance(p, LLMUserAggregator))
    return service, user.context


def _tool_names(context: Any) -> set[str]:
    return {f.name for f in context.tools.standard_tools} if context.tools else set()


def _instructions(service: Any) -> str:
    settings = service._settings
    if hasattr(settings, "session_properties"):  # OpenAI Realtime
        return settings.session_properties.instructions
    return settings.system_instruction


# --- Seam 1: the pipeline build ----------------------------------------------


class TestToolModeOnOpenAILive:
    def test_the_delegate_holds_it_and_gpt_live_1_is_told(self) -> None:
        live, context = _build(
            _config("openai_live", tools=[_BOOK]),
            [_attachment("tool", tool_description="Search the menu and hours.")],
        )

        delegate = live._delegation.backend
        assert set(delegate.llm._functions) == {"book_table", "query_knowledge"}
        assert _tool_names(delegate.context) == {"book_table", "query_knowledge"}
        # gpt-live-1 calls no tools: its own context stays empty, and its
        # instructions name retrieval as something it can delegate.
        assert not context.tools
        assert "- query_knowledge: Search the menu and hours." in (
            live._settings.system_instruction
        )

    def test_a_tool_kb_alone_gets_a_client_delegate(self) -> None:
        from pipecat.services.openai.live.llm import ClientDelegation

        live, _ = _build(_config("openai_live"), [_attachment("tool")])

        assert isinstance(live._delegation, ClientDelegation)
        assert set(live._delegation.backend.llm._functions) == {"query_knowledge"}

    def test_every_failover_member_holds_it(self) -> None:
        live, _ = _build(
            _config("openai_live", llm={"failover": [{"provider": "anthropic"}]}),
            [_attachment("tool")],
        )

        group = live._delegation.backend.llm
        assert len(group.llms) == 2
        for member in group.llms:
            assert set(member._functions) == {"query_knowledge"}

    def test_responses_mode_runs_it_in_the_call(self) -> None:
        """The hosted delegate's function calls are run by the live service,
        with handlers on it — so the tool is in its context, as on Realtime."""
        live, context = _build(
            _config(
                "openai_live",
                s2s={"delegation": {"mode": "responses", "model": "gpt-5-mini"}},
            ),
            [_attachment("tool")],
        )

        assert "query_knowledge" in live._functions
        assert _tool_names(context) == {"query_knowledge"}
        assert "- query_knowledge:" in live._settings.system_instruction


@pytest.mark.parametrize("provider", ["openai", "google", "aws"])
def test_tool_mode_on_a_provider_that_calls_tools_itself(provider: str) -> None:
    service, context = _build(_config(provider, tools=[_BOOK]), [_attachment("tool")])

    assert "query_knowledge" in service._functions
    assert _tool_names(context) == {"book_table", "query_knowledge"}


def test_cascade_tool_mode_is_unchanged() -> None:
    """Cascade now builds query_knowledge through the same helper."""
    llm, context = _build(
        AgentConfig.model_validate({"tools": [_BOOK]}), [_attachment("tool")]
    )

    assert "query_knowledge" in llm._functions
    assert _tool_names(context) == {"book_table", "query_knowledge"}


@pytest.mark.parametrize("provider", ["openai", "google", "aws", "openai_live"])
def test_prompt_mode_text_leads_the_instructions(provider: str) -> None:
    service, _ = _build(_config(provider), [_attachment("prompt")], preamble=PREAMBLE)

    assert _instructions(service).startswith(f"{PREAMBLE}\n\nYou are a receptionist.")


# --- Seam 2: the assembly ----------------------------------------------------


async def _assemble(
    config: AgentConfig, attachments: list[dict[str, Any]]
) -> tuple[Any, Any]:
    """Run build_call_pipeline; return the create_pipeline and preamble mocks."""
    from turncall.orchestrator import pipeline_builder

    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = []
    preamble = AsyncMock(return_value="P")
    with (
        patch.object(pipeline_builder, "create_pipeline", return_value=pipeline) as cp,
        patch.object(pipeline_builder, "CallSession"),
        patch.object(
            pipeline_builder,
            "load_call_credentials",
            new=AsyncMock(return_value=Credentials(platform={"openai": "k"})),
        ),
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=AsyncMock(return_value=attachments),
        ),
        patch("turncall.services.retrieval.build_knowledge_preamble", preamble),
    ):
        await pipeline_builder.build_call_pipeline(
            config=config,
            transport="T",
            call_context=SimpleNamespace(
                agent_id="a",
                project_id="p",
                is_eval=True,
                records=False,
                has_database=True,
            ),
            settings=SimpleNamespace(pipecat=None, byom=None),
            session_factory="SF",
        )
    return cp, preamble


@pytest.mark.asyncio
async def test_auto_mode_is_dropped_on_s2s_and_said() -> None:
    """Dropped before the preamble too: its awareness hint would tell the
    model it can answer from a knowledge base it has no way to read."""
    from loguru import logger

    auto, tool = _attachment("auto"), _attachment("tool")
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        cp, preamble = await _assemble(_config("google"), [auto, tool])
    finally:
        logger.remove(sink)

    assert cp.call_args.kwargs["knowledge_base_attachments"] == [tool]
    assert preamble.call_args.args[1] == [tool]
    assert any("'auto'" in m and "google" in m for m in messages), messages


@pytest.mark.asyncio
async def test_cascade_keeps_auto_mode() -> None:
    auto = _attachment("auto")
    cp, _ = await _assemble(AgentConfig(), [auto])
    assert cp.call_args.kwargs["knowledge_base_attachments"] == [auto]


@pytest.mark.asyncio
async def test_the_assembly_checks_the_delegates_key_for_a_tool_kb() -> None:
    from turncall.orchestrator import pipeline_builder
    from turncall.services.credentials import MissingProviderKeyError

    with (
        patch.object(
            pipeline_builder,
            "load_call_credentials",
            new=AsyncMock(return_value=Credentials(platform={"openai": "k"})),
        ),
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=AsyncMock(return_value=[_attachment("tool")]),
        ),
        patch(
            "turncall.services.retrieval.build_knowledge_preamble",
            new=AsyncMock(return_value=""),
        ),
        patch.object(pipeline_builder, "fail_unstarted_call", new=AsyncMock()),
        pytest.raises(MissingProviderKeyError) as raised,
    ):
        await pipeline_builder.build_call_pipeline(
            config=_config("openai_live", llm={"provider": "anthropic"}),
            transport="T",
            call_context=SimpleNamespace(
                agent_id="a",
                project_id="p",
                is_eval=False,
                records=True,
                has_database=True,
            ),
            settings=SimpleNamespace(pipecat=None, byom=None),
            session_factory="SF",
        )

    assert raised.value.accounts == ["anthropic"]


def test_a_tool_kb_alone_needs_the_delegates_key() -> None:
    """The client delegate it brings is the agent's own llm, whose account
    must hold a key — the same check tools and MCP servers already make."""
    config = _config("openai_live", llm={"provider": "anthropic"})
    keys = Credentials(platform={"openai": "sk"})

    assert keys.missing_keys(config) == []
    assert keys.missing_keys(config, has_knowledge_tool=True) == ["anthropic"]


# --- Seam 3: the agent read --------------------------------------------------


class TestTheWarning:
    def _codes(self, config: AgentConfig, modes: list[str]) -> list[dict[str, Any]]:
        from turncall.orchestrator.config_warnings import config_warnings

        return config_warnings(config, kb_modes=modes)

    @pytest.mark.parametrize("provider", ["openai", "google", "aws", "openai_live"])
    def test_auto_on_s2s_names_the_mode_and_provider(self, provider: str) -> None:
        [warning] = self._codes(_config(provider), ["auto", "tool", "prompt"])

        assert warning["code"] == "knowledge_mode_unsupported"
        assert warning["modes"] == ["auto"]
        assert warning["provider"] == provider
        assert "'auto'" in warning["message"] and provider in warning["message"]

    def test_supported_modes_say_nothing(self) -> None:
        assert self._codes(_config("openai_live"), ["tool", "prompt"]) == []
        assert self._codes(AgentConfig(), ["auto", "tool", "prompt"]) == []

    def test_the_agent_read_carries_it(self, app: Any) -> None:
        from datetime import UTC, datetime

        from turncall.api.deps import get_session
        from turncall.auth.context import AuthContext
        from turncall.auth.dependencies import resolve_auth_context
        from turncall.domain.enums import ProjectRole

        project_id, agent_id = uuid4(), uuid4()
        app.app.dependency_overrides[resolve_auth_context] = lambda: AuthContext(
            project_id=project_id, api_key_id=uuid4(), role=ProjectRole.ADMIN
        )
        app.app.dependency_overrides[get_session] = lambda: AsyncMock()
        row = SimpleNamespace(
            id=agent_id,
            project_id=project_id,
            name="support",
            environment="development",
            version=1,
            state="draft",
            config_blob={"pipeline_mode": "s2s", "s2s": {"provider": "openai_live"}},
            created_at=datetime.now(UTC),
            published_at=None,
        )
        with (
            patch(
                "turncall.storage.repositories.agent_repo.get_agent_by_id",
                new=AsyncMock(return_value=row),
            ),
            patch(
                "turncall.storage.repositories.knowledge_repo.kb_modes_by_agent",
                new=AsyncMock(return_value={agent_id: ["auto"]}),
            ),
        ):
            r = app.get(f"/v1/agents/{agent_id}", headers={"Authorization": "Bearer x"})

        assert r.status_code == 200, r.text
        assert [w["code"] for w in r.json()["data"]["warnings"]] == [
            "knowledge_mode_unsupported"
        ]
