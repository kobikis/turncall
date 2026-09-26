"""handoff_to_agent must hand over the tools as well as the prompt.

It swapped the system instruction and cleared the context, but never touched
the advertised tools — so after a handoff the model believed it was agent B
while still holding agent A's tools, and none of B's.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, MCPServerConfig, ToolDefinition


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="http://x/hook",
    )


class _SessionFactory:
    """A real async context manager — an AsyncMock isn't one, so the code
    under test would silently take its except branch instead."""

    def __init__(self, agent):
        self._agent = agent

    def __call__(self):
        return self

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


async def _handoff(target_config: AgentConfig):
    from turncall.orchestrator.tool_bridge import (
        _apply_handoff_context,
        _prepare_handoff,
    )

    agent = SimpleNamespace(
        name="Billing", config_blob=target_config.model_dump(mode="json")
    )
    call_context = SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        session_factory=_SessionFactory(agent),
        mcp_manager=None,
    )
    params = SimpleNamespace(
        llm=MagicMock(),
        context=MagicMock(),
        pipeline_worker=SimpleNamespace(queue_frame=AsyncMock()),
    )

    with (
        patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            new=AsyncMock(return_value=agent),
        ),
        patch("turncall.orchestrator.tool_bridge.register_tools") as register,
    ):
        target = await _prepare_handoff({"agent_id": str(uuid4())}, call_context)
        assert target is not None
        await _apply_handoff_context(target, call_context, params)

    return params, register


@pytest.mark.unit
@pytest.mark.asyncio
async def test_target_agents_tools_are_registered():
    params, register = await _handoff(AgentConfig(tools=[_tool("refund")]))

    registered = register.call_args.args[1]
    assert [t.name for t in registered] == ["refund"]
    assert register.call_args.args[0] is params.llm


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_advertised_tool_set_is_replaced():
    """Registering handlers isn't enough — the provider only offers what the
    tools schema advertises, so it has to be swapped mid-call too."""
    from pipecat.frames.frames import LLMSetToolsFrame

    params, _ = await _handoff(AgentConfig(tools=[_tool("refund")]))

    frames = [c.args[0] for c in params.pipeline_worker.queue_frame.await_args_list]
    tool_frames = [f for f in frames if isinstance(f, LLMSetToolsFrame)]
    assert len(tool_frames) == 1
    assert [f.name for f in tool_frames[0].tools.standard_tools] == ["refund"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_agent_with_no_tools_clears_the_previous_set():
    """The dangerous direction: handing off to a tool-less agent must not
    leave the previous agent's tools callable."""
    from pipecat.frames.frames import LLMSetToolsFrame

    params, _ = await _handoff(AgentConfig())

    frames = [c.args[0] for c in params.pipeline_worker.queue_frame.await_args_list]
    tool_frames = [f for f in frames if isinstance(f, LLMSetToolsFrame)]
    assert len(tool_frames) == 1
    assert not getattr(tool_frames[0].tools, "standard_tools", [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_servers_on_the_target_are_reported_not_silently_ignored():
    """MCP sessions belong to the source agent and aren't re-opened mid-call.
    That's a real limitation, so it gets logged rather than hidden."""
    config = AgentConfig(mcp_servers=[MCPServerConfig(name="crm", url="http://x/mcp")])

    with patch("turncall.orchestrator.tool_bridge.logger") as log:
        await _handoff(config)

    assert any("mcp" in str(c).lower() for c in log.warning.call_args_list), (
        "a target agent's MCP servers going unconnected must be visible"
    )
