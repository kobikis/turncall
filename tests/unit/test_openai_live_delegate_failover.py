"""`llm.failover` for gpt-live-1's client delegate (#246).

A client delegate is an ordinary LLM, so the S2S refusal's reason — the
conversation lives inside the realtime session — does not apply to it. Seam 1
is the agent-config boundary, seam 2 the pipeline build, seam 3 a delegation
running through pipecat's backend worker.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.utils.errors import ErrorCategory
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig
from turncall.orchestrator.llm_failover import FailoverLLM
from turncall.orchestrator.pipeline_factory import create_pipeline

_EMPTY = {"type": "object", "properties": {}}
_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": _EMPTY,
    "webhook_url": "https://example.com/book",
}
_FAILOVER = {"failover": [{"provider": "anthropic"}]}


def _schema(s2s: dict[str, Any], **config: Any) -> AgentConfigSchema:
    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": s2s, "llm": _FAILOVER, **config}
    )


# --- Seam 1: the agent-config boundary ---------------------------------------


@pytest.mark.unit
class TestBoundary:
    def test_accepted_on_a_defaulted_client_delegate(self) -> None:
        schema = _schema({"provider": "openai_live"}, tools=[_BOOK])
        assert schema.llm.failover[0].provider == "anthropic"

    def test_accepted_on_an_mcp_only_client_delegate(self) -> None:
        _schema(
            {"provider": "openai_live"},
            mcp_servers=[{"name": "crm", "transport": "http", "url": "https://x"}],
        )

    def test_accepted_on_an_explicit_client_block(self) -> None:
        _schema({"provider": "openai_live", "delegation": {"mode": "client"}})

    @pytest.mark.parametrize(
        "s2s",
        [
            {"provider": "openai_live"},  # no tools: no delegate to fail over
            {
                "provider": "openai_live",
                "delegation": {"mode": "responses", "model": "gpt-5"},
            },
            {"provider": "openai_live", "extra": {"backend_model": "gpt-5"}},
            {"provider": "openai"},
            {"provider": "google"},
        ],
    )
    def test_refused_everywhere_else_in_s2s(self, s2s: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match=r"llm\.failover"):
            _schema(s2s, tools=[_BOOK] if "delegation" in s2s else [])


@pytest.mark.unit
def test_a_keyless_delegate_backup_is_missing() -> None:
    from turncall.services.credentials import Credentials

    config = AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "openai_live"},
            "llm": _FAILOVER,
            "tools": [_BOOK],
        }
    )
    assert Credentials(platform={"openai": "sk"}).missing_keys(config) == ["anthropic"]


# --- Seam 2: the pipeline build ----------------------------------------------


def _build(config: AgentConfig) -> Any:
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test", anthropic="sk-ant"),
    )
    return next(p for p in pipeline.processors if isinstance(p, OpenAILiveLLMService))


@pytest.mark.unit
def test_the_delegate_is_a_failover_group_whose_members_all_hold_the_tools() -> None:
    live = _build(
        AgentConfig.model_validate(
            {
                "pipeline_mode": "s2s",
                "system_prompt": "You are a receptionist.",
                "s2s": {"provider": "openai_live"},
                "llm": _FAILOVER,
                "tools": [_BOOK],
            }
        )
    )
    group = live._delegation.backend.llm
    assert isinstance(group, FailoverLLM)
    assert live.delegate_llm is group
    assert [group.label(m) for m in group.llms] == [
        "openai/gpt-4o-mini",
        "anthropic/claude-sonnet-5",
    ]
    for member in group.llms:
        assert set(member._functions) == {"book_table"}
        assert member._settings.system_instruction == "You are a receptionist."


@pytest.mark.unit
async def test_the_session_records_a_delegate_failover() -> None:
    """The delegate is outside the call's pipeline, so the session finds the
    group through the live service rather than among its processors."""
    from pipecat.pipeline.pipeline import Pipeline

    from turncall.domain.enums import CallEventType
    from turncall.orchestrator.call_session import CallSession

    group = FailoverLLM([("a/1", _Fake("a")), ("b/2", _Fake("b"))])
    live = SimpleNamespace(delegate_llm=group)
    cs = CallSession(
        call_context=MagicMock(is_eval=False),
        transport=MagicMock(),
        pipeline=Pipeline([]),
    )
    cs._pipeline = SimpleNamespace(processors=[live])
    cs._watch_failover()

    with patch.object(cs, "_log_event", AsyncMock()) as log_event:
        await group._call_event_handler(
            "on_failover", "a/1", "b/2", ErrorFrame(error="401 bad key")
        )
        await asyncio.sleep(0.01)  # pipecat runs event handlers as tasks

    log_event.assert_awaited_once_with(
        CallEventType.PROVIDER_FAILOVER,
        {"role": "llm", "from": "a/1", "to": "b/2", "error": "401 bad key"},
    )


# --- Seam 3: a delegation through pipecat's backend worker -------------------


class _Fake(LLMService):
    """Answers each context with `reply`, or fails it with `fail`."""

    def __init__(self, reply: str, fail: ErrorCategory | None = None) -> None:
        super().__init__()
        self.reply = reply
        self.fail = fail
        self.contexts = 0

    async def process_frame(self, frame: Any, direction: Any) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.contexts += 1
        await self.push_frame(LLMFullResponseStartFrame())
        if self.fail is not None:
            await self.push_error(f"{self.reply} broke", category=self.fail)
        else:
            await self.push_frame(LLMTextFrame(text=self.reply))
        await self.push_frame(LLMFullResponseEndFrame())


@pytest.mark.unit
async def test_an_unusable_delegate_fails_over_and_the_delegation_is_answered() -> None:
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.filters.identity_filter import IdentityFilter
    from pipecat.workers.llm.backend_llm_worker import (
        BackendLLMWorker,
        _delegate_to_backend,
    )
    from pipecat.workers.runner import WorkerRunner

    primary = _Fake("primary", fail=ErrorCategory.AUTHENTICATION)
    backup = _Fake("backup")
    group = FailoverLLM([("openai/gpt-x", primary), ("anthropic/c", backup)])
    switched: list[tuple[str, str]] = []

    @group.event_handler("on_failover")
    async def _on(_g: object, src: str, dst: str, _e: ErrorFrame) -> None:
        switched.append((src, dst))

    backend = BackendLLMWorker(llm=group, name="delegate")
    front = PipelineWorker(Pipeline([IdentityFilter()]), params=PipelineParams())
    runner = WorkerRunner()
    await runner.add_workers(front, backend)

    async def _delegate() -> str:
        try:
            return await _delegate_to_backend(
                front, "delegate", request="book a table", timeout_secs=5
            )
        finally:
            await front.cancel()

    answer = asyncio.create_task(_delegate())
    await asyncio.wait_for(runner.run(), 10)

    assert await answer == "backup"
    assert (primary.contexts, backup.contexts) == (1, 1)
    assert switched == [("openai/gpt-x", "anthropic/c")]
