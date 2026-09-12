"""Limits on what an MCP server can push into a call.

Three settings existed on paper and not in the code path: the per-tool
response cap did nothing at all, the tool cap was per server with no total,
and nothing stopped a server from claiming a built-in's name.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import Tool

from tests.conftest import mcp_tool
from turncall.services.mcp_client import MCPSessionManager


def _settings(**over):
    mcp = SimpleNamespace(
        max_tools_per_server=50,
        max_tools_total=100,
        max_response_bytes=1000,
        stdio_enabled=False,
        stdio_allowed_commands=[],
    )
    for k, v in over.items():
        setattr(mcp, k, v)
    return SimpleNamespace(mcp=mcp, byom=SimpleNamespace(allowed_url_patterns=[]))


def _tool(name: str) -> Tool:
    return mcp_tool(name)


def _result(text: str) -> MagicMock:
    block = SimpleNamespace(text=text)
    return SimpleNamespace(isError=False, content=[block])


def _manager() -> MCPSessionManager:
    return MCPSessionManager(call_id=MagicMock(), project_id=MagicMock())


@pytest.mark.unit
@pytest.mark.asyncio
class TestResponseCap:
    async def _call(self, payload: str, settings) -> str:
        manager = _manager()
        session = SimpleNamespace(call_tool=AsyncMock(return_value=_result(payload)))
        manager._register_discovered(
            [_tool("dump")], server_name="crm", session=session, settings=settings
        )
        with patch("turncall.config.settings.get_settings", return_value=settings):
            return await manager.call_tool("dump", {})

    async def test_a_normal_result_passes_through_untouched(self) -> None:
        out = await self._call('{"ok": true}', _settings())
        assert out == '{"ok": true}'

    async def test_an_oversized_result_does_not_reach_the_context(self) -> None:
        """MCP_MAX_RESPONSE_BYTES was declared in settings and used nowhere, so
        a server returning megabytes went straight into the prompt."""
        out = await self._call("x" * 5000, _settings(max_response_bytes=1000))

        parsed = json.loads(out)
        assert "error" in parsed
        assert len(out.encode()) <= 1000 + 512  # preview plus the wrapper

    async def test_the_model_still_sees_what_the_tool_started_to_say(self) -> None:
        out = await self._call(
            "BEGIN-MARKER" + "y" * 5000, _settings(max_response_bytes=1000)
        )
        assert "BEGIN-MARKER" in json.loads(out)["preview"]


@pytest.mark.unit
class TestToolCaps:
    def test_builtin_names_cannot_be_claimed(self) -> None:
        """Dispatch checks built-ins first, so an MCP tool called end_call was
        advertised to the model and then hung up the call instead."""
        manager = _manager()
        tools = manager._register_discovered(
            [_tool("end_call"), _tool("safe")],
            server_name="crm",
            session="S",
            settings=_settings(),
        )

        assert [t.name for t in tools] == ["safe"]
        assert "end_call" not in manager._tool_refs

    def test_total_cap_applies_across_servers(self) -> None:
        """max_tools_per_server is per server; ten servers at the cap meant
        500 tools in every request."""
        manager = _manager()
        settings = _settings(max_tools_per_server=50, max_tools_total=3)

        first = manager._register_discovered(
            [_tool(f"a{i}") for i in range(2)],
            server_name="one",
            session="S",
            settings=settings,
        )
        second = manager._register_discovered(
            [_tool(f"b{i}") for i in range(5)],
            server_name="two",
            session="S",
            settings=settings,
        )

        assert len(first) == 2
        assert len(second) == 1
        assert len(manager._tool_refs) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settings_expose_the_new_total_cap() -> None:
    """The knob has to be real, or this is the same bug the audit found."""
    from turncall.config.settings import MCPSettings

    assert MCPSettings().max_tools_total > 0
