"""Shared test fixtures."""

import pytest
from fastapi.testclient import TestClient

from turncall.app import create_app
from turncall.config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings (no real DB/Redis connections)."""
    return Settings()


@pytest.fixture
def app(settings: Settings) -> TestClient:
    """FastAPI test client (no lifespan — skips DB/Redis init)."""
    test_app = create_app(settings)

    # Override lifespan to avoid real DB/Redis connections in unit tests
    test_app.router.lifespan_context = None  # type: ignore[assignment]

    return TestClient(test_app, raise_server_exceptions=False)


def mcp_tool(name: str, schema: dict | None = None):
    """Build an mcp Tool without hard-coding which SDK line is installed.

    1.x spells the field `inputSchema`, 2.x `input_schema`. Constructing it
    by the wrong name is a TypeError, so tests that named one would break on
    the other — the code they cover reads both.
    """
    from mcp.types import Tool

    field = "inputSchema" if "inputSchema" in Tool.model_fields else "input_schema"
    return Tool(
        **{"name": name, "description": "d", field: schema or {"type": "object"}}
    )


def mcp_settings(*, allowed_url_patterns: list[str] | None = None, **mcp_over):
    """A Settings stand-in for the MCP tests.

    Built from MCPSettings' own field definitions rather than hand-listed, so a
    field added there can't leave these stubs behind — which is exactly what
    happened when `connect_timeout_seconds` arrived and four separately
    hand-rolled SimpleNamespaces all raised AttributeError.

    Declared defaults only, never the environment: these tests assert on
    specific limits, and a developer's .env must not change the answer.
    """
    from types import SimpleNamespace

    from turncall.config.settings import MCPSettings

    declared = {
        name: field.get_default(call_default_factory=True)
        for name, field in MCPSettings.model_fields.items()
    }
    return SimpleNamespace(
        mcp=SimpleNamespace(**{**declared, **mcp_over}),
        byom=SimpleNamespace(allowed_url_patterns=allowed_url_patterns or []),
    )
