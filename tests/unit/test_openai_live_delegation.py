"""Responses delegation as a first-class `s2s.delegation` block (#243).

gpt-live-1 calls no tools of its own; it hands work to a *delegate*. This slice
makes the responses mode — an OpenAI-hosted delegate — an explicit block on an
`openai_live` agent, keeps `s2s.extra.backend_model` working as that block,
and tells gpt-live-1 when to delegate from the agent's own tool descriptions.

Seam 1 is the agent-config boundary (what a client sees: accepted, or a 422
and its reason). Seam 2 is the pipeline build (how the built live service and
its delegation are configured).
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator.pipeline_factory import create_pipeline
from turncall.orchestrator.tool_bridge import register_tools


def _schema(s2s: dict[str, Any], **config: Any) -> AgentConfigSchema:
    return AgentConfigSchema.model_validate(
        {"pipeline_mode": "s2s", "s2s": s2s, **config}
    )


def _live(**s2s: Any) -> dict[str, Any]:
    return {"provider": "openai_live", **s2s}


_BOOK = {
    "name": "book_table",
    "description": "Book a table at the restaurant.",
    "parameters_schema": {"type": "object", "properties": {}},
    "webhook_url": "https://example.com/book",
}
_END = {
    "name": "end_call",
    "description": "Hang up once the caller is done.",
    "parameters_schema": {"type": "object", "properties": {}},
}


# --- Seam 1: the agent-config boundary ---------------------------------------


@pytest.mark.unit
class TestBoundary:
    def test_responses_block_is_accepted_on_openai_live(self) -> None:
        schema = _schema(
            _live(
                delegation={
                    "mode": "responses",
                    "model": "gpt-6-astra",
                    "reasoning_effort": "low",
                    "service_tier": "priority",
                    "instructions": "Book only on weekdays.",
                    "timeout_seconds": 45,
                }
            )
        )
        delegation = schema.s2s.delegation
        assert delegation is not None
        assert delegation.model == "gpt-6-astra"
        assert delegation.timeout_seconds == 45

    def test_timeout_defaults_to_30(self) -> None:
        schema = _schema(_live(delegation={"mode": "responses", "model": "m"}))
        assert schema.s2s.delegation is not None
        assert schema.s2s.delegation.timeout_seconds == 30

    def test_block_survives_into_the_domain_config(self) -> None:
        """The API stores `model_dump()` and the pipeline reads AgentConfig."""
        schema = _schema(_live(delegation={"mode": "responses", "model": "m"}))
        config = AgentConfig.model_validate(schema.model_dump())
        assert config.s2s.delegation is not None
        assert config.s2s.delegation.model == "m"

    @pytest.mark.parametrize("provider", ["openai", "google", "aws"])
    def test_delegation_on_another_provider_is_refused(self, provider: str) -> None:
        with pytest.raises(ValidationError, match="only openai_live delegates"):
            _schema(
                {
                    "provider": provider,
                    "delegation": {"mode": "responses", "model": "m"},
                }
            )

    def test_responses_mode_without_model_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="needs a model"):
            _schema(_live(delegation={"mode": "responses"}))

    def test_client_mode_is_not_accepted_yet(self) -> None:
        with pytest.raises(ValidationError):
            _schema(_live(delegation={"mode": "client"}))

    def test_unknown_delegation_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _schema(_live(delegation={"mode": "responses", "model": "m", "x": 1}))

    def test_handoff_to_agent_on_openai_live_is_refused(self) -> None:
        handoff = {"name": "handoff_to_agent", "description": "Hand over."}
        with pytest.raises(ValidationError, match="handoff_to_agent"):
            _schema(_live(), tools=[handoff])

    def test_handoff_to_agent_on_realtime_is_still_fine(self) -> None:
        handoff = {"name": "handoff_to_agent", "description": "Hand over."}
        _schema({"provider": "openai"}, tools=[handoff])

    def test_handoff_on_a_cascade_agent_naming_openai_live_is_fine(self) -> None:
        """The s2s block is inert on cascade; refusing there would be noise."""
        handoff = {"name": "handoff_to_agent", "description": "Hand over."}
        AgentConfigSchema.model_validate({"s2s": _live(), "tools": [handoff]})

    def test_a_builder_style_openai_live_agent_still_saves(self) -> None:
        """No delegation block, the fields the console sends."""
        _schema(_live(model="gpt-live-1", voice="alloy"), tools=[_END])


# --- Seam 2: the pipeline build ----------------------------------------------


def _config(s2s: dict[str, Any], tools: list[dict[str, Any]] | None = None):
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "system_prompt": "You are a receptionist.",
            "s2s": s2s,
            "tools": tools or [],
        }
    )


def _build(config: AgentConfig, mcp_tools: list[Any] | None = None) -> Any:
    """Build the real S2S pipeline and return its live service."""
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test"),
        mcp_tools=mcp_tools,
    )
    live = next(
        p
        for p in pipeline.processors_with_metrics()
        if isinstance(p, OpenAILiveLLMService)
    )
    # What build_call_pipeline does next: TurnCall's handlers onto the LLM.
    tools = list(config.tools) + list(mcp_tools or [])
    if tools:
        register_tools(live, tools, MagicMock(is_eval=True))
    return live


@pytest.mark.unit
class TestBuild:
    def test_block_builds_responses_delegation_with_its_settings(self) -> None:
        live = _build(
            _config(
                _live(
                    delegation={
                        "mode": "responses",
                        "model": "gpt-6-astra",
                        "reasoning_effort": "low",
                        "service_tier": "priority",
                    }
                ),
                tools=[_BOOK],
            )
        )
        from pipecat.services.openai.live.llm import ResponsesDelegation

        assert isinstance(live._delegation, ResponsesDelegation)
        assert live._delegation.settings.model == "gpt-6-astra"
        assert live._delegation.settings.reasoning.effort == "low"
        assert live._delegation.service_tier == "priority"

    def test_backend_model_builds_the_same_delegation_as_the_block(self) -> None:
        legacy = _build(_config(_live(extra={"backend_model": "gpt-6-astra"})))
        block = _build(
            _config(_live(delegation={"mode": "responses", "model": "gpt-6-astra"}))
        )
        assert legacy._delegation == block._delegation
        assert legacy._function_call_timeout_secs == block._function_call_timeout_secs
        assert legacy._settings.system_instruction == block._settings.system_instruction

    def test_no_delegation_without_a_block_or_backend_model(self) -> None:
        live = _build(_config(_live(), tools=[_BOOK]))
        assert live._delegation is None

    def test_timeout_defaults_to_30_and_reaches_the_service(self) -> None:
        live = _build(_config(_live(delegation={"mode": "responses", "model": "m"})))
        assert live._function_call_timeout_secs == 30

    def test_timeout_is_settable(self) -> None:
        live = _build(
            _config(
                _live(
                    delegation={
                        "mode": "responses",
                        "model": "m",
                        "timeout_seconds": 90,
                    }
                )
            )
        )
        assert live._function_call_timeout_secs == 90

    def test_system_prompt_reaches_the_delegate(self) -> None:
        live = _build(_config(_live(delegation={"mode": "responses", "model": "m"})))
        assert live._delegation.settings.system_instruction == "You are a receptionist."

    def test_delegation_instructions_replace_the_delegates_prompt(self) -> None:
        live = _build(
            _config(
                _live(
                    delegation={
                        "mode": "responses",
                        "model": "m",
                        "instructions": "Book only on weekdays.",
                    }
                )
            )
        )
        assert live._delegation.settings.system_instruction == "Book only on weekdays."
        # ...and only the delegate's: gpt-live-1 keeps the agent's prompt.
        assert "You are a receptionist." in live._settings.system_instruction

    def test_live_instructions_name_every_tool_static_and_mcp(self) -> None:
        crm = ToolDefinition(
            name="crm_lookup",
            description="Look a customer up in the CRM.",
            parameters_schema={"type": "object", "properties": {}},
        )
        live = _build(
            _config(
                _live(delegation={"mode": "responses", "model": "m"}),
                tools=[_BOOK, _END],
            ),
            mcp_tools=[crm],
        )
        instructions = live._settings.system_instruction
        assert instructions.startswith("You are a receptionist.")
        assert "delegate" in instructions.lower()
        for name, description in [
            ("book_table", "Book a table at the restaurant."),
            ("end_call", "Hang up once the caller is done."),
            ("crm_lookup", "Look a customer up in the CRM."),
        ]:
            assert f"{name}: {description}" in instructions

    def test_no_delegation_paragraph_without_delegation(self) -> None:
        """Pipecat declines every delegation then; telling the model to
        delegate would only promise the caller work that never happens."""
        live = _build(_config(_live(), tools=[_BOOK]))
        assert live._settings.system_instruction == "You are a receptionist."

    def test_delegate_holds_the_agent_tools_with_turncall_handlers(self) -> None:
        """Responses mode: the live service runs the delegate's function
        calls, so TurnCall's handlers are registered on it."""
        live = _build(
            _config(
                _live(delegation={"mode": "responses", "model": "m"}),
                tools=[_BOOK, _END],
            )
        )
        assert {"book_table", "end_call"} <= set(live._functions)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegated_end_call_arms_the_calls_hangup_gate() -> None:
    """Responses mode: the delegate's `end_call` runs on the live service, in
    the call's pipeline, so it arms #240's gate there — the call ends after
    gpt-live-1's goodbye. The gate is the very next stage after the service."""
    import json
    import uuid
    from unittest.mock import AsyncMock, patch

    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    from turncall.orchestrator import tool_bridge
    from turncall.orchestrator.hangup import HangupAfterReply, HangupArmFrame
    from turncall.orchestrator.pipeline_factory import CallContext

    config = _config(
        _live(delegation={"mode": "responses", "model": "m"}), tools=[_END]
    )
    call_context = CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="CA1",
        stream_sid="MZ1",
        session_factory=MagicMock(),
    )
    with patch("turncall.orchestrator.call_recorder.attach_recorder"):
        pipeline = create_pipeline(
            config=config,
            transport=MagicMock(),
            call_context=call_context,
            pipecat_settings=SimpleNamespace(),
            credentials=creds("sk-test"),
        )
    stages = pipeline.processors
    live = next(p for p in stages if isinstance(p, OpenAILiveLLMService))
    assert isinstance(stages[stages.index(live) + 1], HangupAfterReply)

    pushed: list[Any] = []

    async def _push(frame: Any, *_: Any) -> None:
        pushed.append(frame)

    ok = json.dumps({"success": True, "message": "ok", "details": None})
    with (
        patch.object(tool_bridge, "_execute_builtin", new=AsyncMock(return_value=ok)),
        patch.object(tool_bridge, "_spawn"),
        patch.object(live, "push_frame", new=_push),
    ):
        register_tools(live, list(config.tools), call_context)
        await live._functions["end_call"].handler(
            SimpleNamespace(
                function_name="end_call",
                arguments={},
                result_callback=AsyncMock(),
                llm=live,
            )
        )
    assert [type(f) for f in pushed] == [HangupArmFrame]


@pytest.mark.unit
def test_call_init_variables_reach_the_delegates_instructions() -> None:
    from turncall.services.template_renderer import render_agent_config

    config = _config(
        _live(
            delegation={
                "mode": "responses",
                "model": "m",
                "instructions": "The caller is {{name}}.",
            }
        )
    )
    rendered = render_agent_config(config, {"name": "Jane"})
    assert rendered.s2s.delegation is not None
    assert rendered.s2s.delegation.instructions == "The caller is Jane."


@pytest.mark.unit
def test_empty_delegate_instructions_are_refused() -> None:
    with pytest.raises(ValidationError):
        _schema(
            _live(delegation={"mode": "responses", "model": "m", "instructions": ""})
        )
