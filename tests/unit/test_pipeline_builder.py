"""build_call_pipeline: the shared assembly extracted from the three voice
transports. Verifies KB attachments + transport options are forwarded to
create_pipeline and tools are registered on the LLM service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.domain.models import AgentConfig
from turncall.orchestrator import pipeline_builder


def _settings():
    return SimpleNamespace(
        openai=SimpleNamespace(api_key="oa"),
        pipecat=SimpleNamespace(),
        byom=SimpleNamespace(),
        google=SimpleNamespace(api_key="g"),
        anthropic=SimpleNamespace(api_key="a"),
        openrouter=SimpleNamespace(api_key="or"),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forwards_kb_and_transport_options():
    ctx = SimpleNamespace(agent_id="a1")
    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = []  # no LLM proc -> skip tools

    with (
        patch.object(pipeline_builder, "create_pipeline", return_value=pipeline) as cp,
        patch.object(pipeline_builder, "CallSession", return_value="SESSION") as cs,
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=AsyncMock(return_value=[{"kb": 1}]),
        ),
    ):
        out = await pipeline_builder.build_call_pipeline(
            config=AgentConfig(),
            transport="T",
            call_context=ctx,
            settings=_settings(),
            session_factory="SF",
            audio_sample_rate=16000,
            mcp_tools=["m1"],
            avatar_enabled=True,
        )

    kw = cp.call_args.kwargs
    assert kw["knowledge_base_attachments"] == [{"kb": 1}]  # KB stays wired
    assert kw["audio_sample_rate"] == 16000
    assert kw["mcp_tools"] == ["m1"]
    assert kw["avatar_enabled"] is True
    assert out == "SESSION"
    assert cs.call_args.kwargs["transport"] == "T"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registers_static_plus_mcp_tools():
    ctx = SimpleNamespace(agent_id="a1")

    class FakeLLM:
        pass

    llm = FakeLLM()
    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = [object(), llm]

    cfg = AgentConfig(
        tools=[{"name": "end_call", "description": "e", "parameters_schema": {"type": "object"}}]
    )

    with (
        patch.object(pipeline_builder, "create_pipeline", return_value=pipeline),
        patch.object(pipeline_builder, "CallSession", return_value="S"),
        patch.object(pipeline_builder, "register_tools") as reg,
        patch("pipecat.services.llm_service.LLMService", FakeLLM),
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await pipeline_builder.build_call_pipeline(
            config=cfg,
            transport="T",
            call_context=ctx,
            settings=_settings(),
            session_factory="SF",
            mcp_tools=["mcp_tool"],
        )

    reg.assert_called_once()
    proc_arg, tools_arg, _ctx = reg.call_args.args
    assert proc_arg is llm  # registered on the LLM proc, not the other one
    assert len(tools_arg) == 2  # static (1) + mcp (1)
