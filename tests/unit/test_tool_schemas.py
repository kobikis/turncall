"""Tests for tool registration schemas."""

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.tools import RegisterToolRequest


@pytest.mark.unit
class TestRegisterToolRequest:
    def test_valid_tool(self) -> None:
        tool = RegisterToolRequest(
            name="lookup_customer",
            description="Look up customer by ID",
            parameters_schema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
            },
            webhook_url="https://api.example.com/tools/lookup",
        )
        assert tool.name == "lookup_customer"

    def test_minimal_tool(self) -> None:
        tool = RegisterToolRequest(
            name="simple_tool",
            description="A simple tool",
        )
        assert tool.execution_mode == "sync"
        assert tool.timeout_seconds == 10

    def test_invalid_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegisterToolRequest(
                name="Invalid-Name",
                description="Bad",
            )

    def test_async_mode(self) -> None:
        tool = RegisterToolRequest(
            name="async_tool",
            description="An async tool",
            execution_mode="async",
        )
        assert tool.execution_mode == "async"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegisterToolRequest(
                name="bad_tool",
                description="Bad",
                execution_mode="invalid",
            )
