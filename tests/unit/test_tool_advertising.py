"""What the model is actually told it can call.

Two gaps found auditing MCP across the transports: S2S connected MCP servers
and then never advertised their tools, and nothing deduplicated tool names —
so two servers exposing a common name (`search`, `get`) put duplicate function
names in one request.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import mcp_settings, mcp_tool
from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.orchestrator.pipeline_factory import _build_tools_schema, create_pipeline


def _tool(name: str, description: str = "d") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters_schema={"type": "object", "properties": {}},
    )


@pytest.mark.unit
def test_s2s_advertises_mcp_tools():
    """S2S agents connected their MCP servers, paid the handshake, held the
    session for the whole call — and never told the model the tools existed."""
    config = AgentConfig(pipeline_mode="s2s")

    with patch("turncall.orchestrator.pipeline_factory._create_s2s_pipeline") as s2s:
        create_pipeline(
            config=config,
            transport=MagicMock(),
            call_context=MagicMock(),
            openai_api_key="k",
            pipecat_settings=SimpleNamespace(),
            mcp_tools=[_tool("crm_lookup")],
        )

    assert s2s.call_args.kwargs["mcp_tools"] == [_tool("crm_lookup")]


@pytest.mark.unit
def test_schema_includes_both_static_and_mcp_tools():
    schema = _build_tools_schema(
        AgentConfig(tools=[_tool("book")]), extra_tools=[_tool("crm_lookup")]
    )
    assert [f.name for f in schema.standard_tools] == ["book", "crm_lookup"]


@pytest.mark.unit
def test_duplicate_names_are_dropped_not_advertised_twice():
    """Two MCP servers both exposing `search` used to produce two identical
    function names in one request, which providers reject outright."""
    schema = _build_tools_schema(
        AgentConfig(),
        extra_tools=[_tool("search", "from crm"), _tool("search", "from docs")],
    )

    names = [f.name for f in schema.standard_tools]
    assert names == ["search"]
    # First registration wins, matching MCPSessionManager's own ref map.
    assert schema.standard_tools[0].description == "from crm"


@pytest.mark.unit
def test_an_mcp_tool_cannot_shadow_a_configured_tool():
    """Static tools are the customer's own and take precedence — an MCP server
    can't silently take over a name the agent config already uses."""
    schema = _build_tools_schema(
        AgentConfig(tools=[_tool("lookup", "mine")]),
        extra_tools=[_tool("lookup", "theirs")],
    )

    assert [f.name for f in schema.standard_tools] == ["lookup"]
    assert schema.standard_tools[0].description == "mine"


@pytest.mark.unit
def test_mcp_discovery_skips_a_name_another_server_already_claimed():
    """The ref map is keyed by bare tool name, so the second server used to
    overwrite the first — advertising a tool that routed somewhere else."""

    from turncall.services.mcp_client import MCPSessionManager

    manager = MCPSessionManager(call_id=MagicMock(), project_id=MagicMock())
    settings = mcp_settings()

    def _mcp_tool(name: str):
        return mcp_tool(name)

    first = manager._register_discovered(
        [_mcp_tool("search")], server_name="crm", session="S1", settings=settings
    )
    second = manager._register_discovered(
        [_mcp_tool("search")], server_name="docs", session="S2", settings=settings
    )

    assert [t.name for t in first] == ["search"]
    assert second == []
    assert manager._tool_refs["search"].server_name == "crm"
