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
