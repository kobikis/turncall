"""MCP server URLs go through the same allowlist as custom LLM endpoints.

`llm.base_url` and `s2s.base_url` are both gated — pipeline_factory calls a
gateway base_url "an attacker-influenceable outbound target". An MCP url is
the same kind of target, reached the same way, from the same process, and was
the only one of the three with no check at all.
"""

import pytest

from tests.conftest import mcp_settings
from turncall.domain.models import MCPServerConfig
from turncall.services.mcp_client import MCPSessionManager
from turncall.services.url_allowlist import check_url_allowed


def _settings(patterns: list[str]):
    return mcp_settings(
        allowed_url_patterns=patterns,
        stdio_enabled=False,
        stdio_allowed_commands=[],
    )


@pytest.mark.unit
class TestCheckUrlAllowed:
    def test_empty_patterns_allow_everything(self) -> None:
        """Dev mode, and the existing BYOM behaviour — an operator opts in by
        setting BYOM_ALLOWED_URL_PATTERNS."""
        check_url_allowed("http://169.254.169.254/latest/meta-data/", [])

    def test_matching_pattern_passes(self) -> None:
        check_url_allowed("https://mcp.example.com/mcp", ["https://mcp.example.com/*"])

    def test_non_matching_pattern_raises(self) -> None:
        with pytest.raises(ValueError, match="not in allowed patterns"):
            check_url_allowed(
                "http://169.254.169.254/latest/meta-data/",
                ["https://mcp.example.com/*"],
            )

    def test_label_names_the_offending_field(self) -> None:
        with pytest.raises(ValueError, match="MCP server 'crm' url"):
            check_url_allowed(
                "http://internal/", ["https://ok/*"], label="MCP server 'crm' url"
            )


@pytest.mark.unit
@pytest.mark.asyncio
class TestMCPSessionAllowlist:
    async def test_blocked_url_never_opens_a_session(self) -> None:
        manager = MCPSessionManager(call_id="c", project_id="p")
        server = MCPServerConfig(
            name="crm",
            transport="http",
            url="http://169.254.169.254/latest/meta-data/",
        )

        with pytest.raises(ValueError, match="not in allowed patterns"):
            await manager._create_session(server, _settings(["https://mcp.ok/*"]))

    async def test_sse_transport_is_gated_too(self) -> None:
        manager = MCPSessionManager(call_id="c", project_id="p")
        server = MCPServerConfig(
            name="crm", transport="sse", url="http://internal.svc/sse"
        )

        with pytest.raises(ValueError, match="not in allowed patterns"):
            await manager._create_session(server, _settings(["https://mcp.ok/*"]))

    async def test_a_blocked_server_does_not_sink_the_call(self) -> None:
        """connect_servers logs and continues per server, so one bad entry
        must not cost the caller the tools from the good ones."""
        from unittest.mock import patch

        manager = MCPSessionManager(call_id="c", project_id="p")
        servers = [
            MCPServerConfig(name="bad", transport="http", url="http://169.254.169.254/")
        ]

        with patch(
            "turncall.config.settings.get_settings",
            return_value=_settings(["https://mcp.ok/*"]),
        ):
            tools = await manager.connect_servers(servers)

        assert tools == []
