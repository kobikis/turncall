"""start_call_pipeline: MCP discovery for the fire-and-forget transports.

WebRTC and WhatsApp voice spawn the pipeline in a task, so they can't connect
MCP servers the way media_stream does (inline, then `await start()`): an MCP
transport opens anyio cancel scopes that must be exited in the task that
entered them, and CallSession closes the manager during its own cleanup.
These tests pin that invariant plus the wiring that was simply missing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, MCPServerConfig
from turncall.orchestrator import pipeline_builder
from turncall.orchestrator.pipeline_factory import CallContext


def _settings():
    return SimpleNamespace(
        openai=SimpleNamespace(api_key="oa"),
        pipecat=SimpleNamespace(),
        byom=SimpleNamespace(),
        google=SimpleNamespace(api_key="g"),
        anthropic=SimpleNamespace(api_key="a"),
        openrouter=SimpleNamespace(api_key="or"),
    )


def _ctx():
    return CallContext(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory="SF",
    )


class _Session:
    """Stand-in CallSession that records the task its run happened in."""

    def __init__(self):
        self.started = asyncio.Event()
        self.task = None

    async def start(self):
        self.task = asyncio.current_task()
        self.started.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_mcp_servers_skips_the_manager():
    session = _Session()
    with (
        patch.object(
            pipeline_builder, "build_call_pipeline", new=AsyncMock(return_value=session)
        ) as build,
        patch("turncall.services.mcp_client.MCPSessionManager") as manager_cls,
    ):
        await pipeline_builder.start_call_pipeline(
            config=AgentConfig(),
            transport="T",
            call_context=_ctx(),
            settings=_settings(),
            session_factory="SF",
            audio_sample_rate=16000,
        )
        await asyncio.wait_for(session.started.wait(), 1)

    manager_cls.assert_not_called()
    assert build.call_args.kwargs["mcp_tools"] is None
    assert build.call_args.kwargs["audio_sample_rate"] == 16000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discovers_mcp_tools_and_puts_the_manager_on_the_context():
    """The bug: WebRTC/WhatsApp never connected MCP, so an agent with
    mcp_servers got zero tools and tool_bridge had no manager to route to."""
    session = _Session()
    manager = SimpleNamespace(
        connect_servers=AsyncMock(return_value=["tool-a"]), close=AsyncMock()
    )
    config = AgentConfig(mcp_servers=[MCPServerConfig(name="crm", url="http://x/mcp")])

    with (
        patch.object(
            pipeline_builder, "build_call_pipeline", new=AsyncMock(return_value=session)
        ) as build,
        patch(
            "turncall.services.mcp_client.MCPSessionManager", return_value=manager
        ),
    ):
        await pipeline_builder.start_call_pipeline(
            config=config,
            transport="T",
            call_context=_ctx(),
            settings=_settings(),
            session_factory="SF",
        )
        await asyncio.wait_for(session.started.wait(), 1)

    assert manager.connect_servers.await_args.args[0] == config.mcp_servers
    assert build.call_args.kwargs["mcp_tools"] == ["tool-a"]
    assert build.call_args.kwargs["call_context"].mcp_manager is manager


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connect_and_run_share_one_task():
    """anyio cancel scopes are task-bound: whoever opens the MCP transport
    must be the one that closes it, and CallSession closes it inside start()."""
    session = _Session()
    connect_task = {}

    async def _connect(_servers):
        connect_task["t"] = asyncio.current_task()
        return []

    manager = SimpleNamespace(connect_servers=_connect, close=AsyncMock())
    config = AgentConfig(mcp_servers=[MCPServerConfig(name="crm", url="http://x/mcp")])

    with (
        patch.object(
            pipeline_builder, "build_call_pipeline", new=AsyncMock(return_value=session)
        ),
        patch("turncall.services.mcp_client.MCPSessionManager", return_value=manager),
    ):
        await pipeline_builder.start_call_pipeline(
            config=config,
            transport="T",
            call_context=_ctx(),
            settings=_settings(),
            session_factory="SF",
        )
        await asyncio.wait_for(session.started.wait(), 1)

    assert connect_task["t"] is session.task
    assert connect_task["t"] is not asyncio.current_task()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_failure_propagates_and_closes_mcp():
    """The caller still sees the error (WebRTC returns 500 on it), and the
    MCP sessions don't leak — CallSession.cleanup never runs on this path."""
    manager = SimpleNamespace(
        connect_servers=AsyncMock(return_value=[]), close=AsyncMock()
    )
    config = AgentConfig(mcp_servers=[MCPServerConfig(name="crm", url="http://x/mcp")])

    with (
        patch.object(
            pipeline_builder,
            "build_call_pipeline",
            new=AsyncMock(side_effect=RuntimeError("bad provider")),
        ),
        patch("turncall.services.mcp_client.MCPSessionManager", return_value=manager),
        pytest.raises(RuntimeError, match="bad provider"),
    ):
        await pipeline_builder.start_call_pipeline(
            config=config,
            transport="T",
            call_context=_ctx(),
            settings=_settings(),
            session_factory="SF",
        )

    manager.close.assert_awaited_once()
