"""Client delegation: the agent's own `llm` as gpt-live-1's delegate (#244).

An `openai_live` agent with tools and no `s2s.delegation` block now delegates
to its own `llm`, run in pipecat's backend worker with TurnCall's handlers on
it. Seam 1 is the agent-config boundary, seam 2 the pipeline build.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline
from turncall.services.credentials import Credentials
from turncall.services.tool_mocks import ToolMocks

_EMPTY = {"type": "object", "properties": {}}
_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": _EMPTY,
    "webhook_url": "https://example.com/book",
}


def _paired(prompt: str) -> str:
    """The delegate's prompt, with pipecat's pairing instruction after it (1.13)."""
    from pipecat.workers.llm.backend_llm_worker import BACKEND_PAIRING_INSTRUCTION

    return f"{prompt}\n\n{BACKEND_PAIRING_INSTRUCTION}"


def _builtin(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name}.", "parameters_schema": _EMPTY}


def _schema(s2s: dict[str, Any], **config: Any) -> AgentConfigSchema:
    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": "openai_live", **s2s}, **config}
    )


# --- Seam 1: the agent-config boundary ---------------------------------------


@pytest.mark.unit
class TestBoundary:
    def test_client_block_is_accepted(self) -> None:
        schema = _schema(
            {"delegation": {"mode": "client", "timeout_seconds": 45}}, tools=[_BOOK]
        )
        assert schema.s2s.delegation is not None
        assert schema.s2s.delegation.mode == "client"

    @pytest.mark.parametrize(
        "field", [("model", "m"), ("reasoning_effort", "low"), ("service_tier", "flex")]
    )
    def test_client_mode_refuses_the_hosted_delegates_settings(
        self, field: tuple[str, str]
    ) -> None:
        with pytest.raises(ValidationError, match=f"client mode takes no {field[0]}"):
            _schema({"delegation": {"mode": "client", field[0]: field[1]}})

    @pytest.mark.parametrize("name", ["end_call", "do_not_call", "transfer_call"])
    def test_call_control_is_accepted_in_client_mode(self, name: str) -> None:
        """The #244 refusal is gone: the delegate arms the call's gate (#245)."""
        _schema({}, tools=[_BOOK, _builtin(name)])
        _schema({"delegation": {"mode": "client"}}, tools=[_builtin(name)])


# --- Seam 2: the pipeline build ----------------------------------------------


_CRM = ToolDefinition(
    name="crm_lookup",
    description="Look a customer up in the CRM.",
    parameters_schema=_EMPTY,
)


def _config(s2s: dict[str, Any] | None = None, **config: Any) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "system_prompt": "You are a receptionist.",
            "s2s": {"provider": "openai_live", **(s2s or {})},
            **config,
        }
    )


def _build(
    config: AgentConfig,
    call_context: Any = None,
    credentials: Credentials | None = None,
    mcp_tools: list[Any] | None = None,
) -> tuple[Any, Any]:
    """Build the real S2S pipeline; return the live service and its context."""
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregator,
    )
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=call_context or MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=credentials or creds("sk-test"),
        mcp_tools=mcp_tools,
    )
    stages = pipeline.processors_with_metrics()
    live = next(p for p in stages if isinstance(p, OpenAILiveLLMService))
    user = next(p for p in pipeline.processors if isinstance(p, LLMUserAggregator))
    return live, user.context


@pytest.mark.unit
class TestBuild:
    def test_tools_without_a_block_delegate_to_the_agents_llm(self) -> None:
        from pipecat.services.openai.live.llm import ClientDelegation
        from pipecat.workers.llm.backend_llm_worker import BackendLLMWorker

        live, context = _build(_config(tools=[_BOOK]), mcp_tools=[_CRM])
        assert isinstance(live._delegation, ClientDelegation)
        assert isinstance(live._delegation.backend, BackendLLMWorker)
        assert live._delegation.timeout_secs == 30
        delegate = live._delegation.backend
        # The delegate holds static + MCP tools, with TurnCall's handlers...
        assert set(delegate.llm._functions) == {"book_table", "crm_lookup"}
        assert {f.name for f in delegate.context.tools.standard_tools} == {
            "book_table",
            "crm_lookup",
        }
        # ...and the live service's context holds none.
        assert not context.tools
        assert "- crm_lookup: Look a customer up in the CRM." in (
            live._settings.system_instruction
        )

    def test_client_block_builds_the_same_with_its_timeout(self) -> None:
        live, _ = _build(
            _config(
                {"delegation": {"mode": "client", "timeout_seconds": 90}}, tools=[_BOOK]
            )
        )
        assert live._delegation.timeout_secs == 90
        assert "book_table" in live._delegation.backend.llm._functions

    def test_no_tools_no_delegation(self) -> None:
        live, _ = _build(_config())
        assert live._delegation is None

    def test_the_delegate_is_the_agents_own_llm(self) -> None:
        from pipecat.services.anthropic.llm import AnthropicLLMService

        live, _ = _build(
            _config(tools=[_BOOK], llm={"provider": "anthropic"}),
            credentials=creds("sk-test", anthropic="sk-ant"),
        )
        delegate_llm = live._delegation.backend.llm
        assert isinstance(delegate_llm, AnthropicLLMService)
        assert delegate_llm._settings.system_instruction == _paired(
            "You are a receptionist."
        )

    def test_delegation_instructions_replace_the_delegates_prompt(self) -> None:
        live, _ = _build(
            _config(
                {"delegation": {"mode": "client", "instructions": "Weekdays only."}},
                tools=[_BOOK],
            )
        )
        delegate_llm = live._delegation.backend.llm
        assert delegate_llm._settings.system_instruction == _paired("Weekdays only.")
        assert live._settings.system_instruction.startswith("You are a receptionist.")


def _call_context(**overrides: Any) -> CallContext:
    return CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        stream_sid="MZ1",
        session_factory=MagicMock(),
        **overrides,
    )


def _params(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        function_name=name, arguments={}, result_callback=AsyncMock(), llm=None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_delegated_tool_is_mocked_in_an_eval() -> None:
    mocks = ToolMocks(responses={"book_table": {"booked": True}})
    call_context = _call_context(eval_run_id=uuid.uuid4(), tool_mocks=mocks)
    live, _ = _build(_config(tools=[_BOOK]), call_context=call_context)

    params = _params("book_table")
    await live._delegation.backend.llm._functions["book_table"].handler(params)

    assert json.loads(params.result_callback.await_args.args[0]) == {"booked": True}
    assert [c["tool_name"] for c in mocks.calls] == ["book_table"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_delegated_tool_is_recorded_on_a_call() -> None:
    from turncall.orchestrator import tool_bridge

    call_context = _call_context()
    live, _ = _build(_config(tools=[_BOOK]), call_context=call_context)

    with (
        patch.object(
            tool_bridge, "_execute_webhook_tool", new=AsyncMock(return_value="ok")
        ),
        patch.object(tool_bridge, "_spawn") as spawn,
        patch.object(tool_bridge, "_log_tool_result", new=MagicMock()) as log,
    ):
        await live._delegation.backend.llm._functions["book_table"].handler(
            _params("book_table")
        )
    spawn.assert_called_once()
    assert log.call_args.args[:2] == (call_context, "book_table")


@pytest.mark.unit
class TestMissingKeys:
    def test_a_keyless_client_delegate_is_missing(self) -> None:
        config = _config(tools=[_BOOK], llm={"provider": "anthropic"})
        assert Credentials(platform={"openai": "sk"}).missing_keys(config) == [
            "anthropic"
        ]

    def test_mcp_servers_count_as_tools(self) -> None:
        config = _config(
            llm={"provider": "anthropic"},
            mcp_servers=[{"name": "crm", "transport": "http", "url": "https://x"}],
        )
        assert Credentials(platform={"openai": "sk"}).missing_keys(config) == [
            "anthropic"
        ]

    def test_no_delegate_no_key_needed(self) -> None:
        config = _config(llm={"provider": "anthropic"})
        assert Credentials(platform={"openai": "sk"}).missing_keys(config) == []

    def test_a_responses_delegate_needs_no_llm_key(self) -> None:
        config = _config(
            {"delegation": {"mode": "responses", "model": "m"}},
            tools=[_BOOK],
            llm={"provider": "anthropic"},
        )
        assert Credentials(platform={"openai": "sk"}).missing_keys(config) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["end_call", "do_not_call"])
async def test_the_delegates_ending_tool_marks_the_call_as_ending(name: str) -> None:
    """The delegate runs in its own worker, so pushing from its LLM would arm
    nothing: the handler marks the call's gate as ending itself (#245). It
    does not arm it — the live service does, on relaying the delegate's
    goodbye, so an acknowledgement gpt-live-1 gives meanwhile is not taken
    for the goodbye (#262)."""
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    from turncall.orchestrator import tool_bridge
    from turncall.orchestrator.hangup import HangupAfterReply

    call_context = _call_context()
    with patch("turncall.orchestrator.call_recorder.attach_recorder"):
        pipeline = create_pipeline(
            config=_config(tools=[_builtin(name)]),
            transport=MagicMock(),
            call_context=call_context,
            pipecat_settings=SimpleNamespace(),
            credentials=creds("sk-test"),
        )
    gate = next(p for p in pipeline.processors if isinstance(p, HangupAfterReply))
    live = next(p for p in pipeline.processors if isinstance(p, OpenAILiveLLMService))
    delegate_llm = live._delegation.backend.llm

    queued: list[Any] = []

    async def _queue(frame: Any, *_: Any, **__: Any) -> None:
        queued.append(frame)

    ok = json.dumps({"success": True, "message": "ok", "details": None})
    execute = AsyncMock(return_value=ok)
    params = _params(name)
    params.llm = delegate_llm
    with (
        patch.object(tool_bridge, "_execute_builtin", new=execute),
        patch.object(tool_bridge, "_spawn"),
        patch.object(gate, "queue_frame", new=_queue),
        patch.object(delegate_llm, "push_frame") as delegate_push,
    ):
        await delegate_llm._functions[name].handler(params)

    assert execute.await_args.args[0] == name  # the end / opt-out is recorded
    assert gate.ending
    assert not queued
    delegate_push.assert_not_called()
    params.result_callback.assert_awaited_once_with(ok)  # the goodbye follows
