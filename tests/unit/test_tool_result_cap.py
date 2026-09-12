"""An oversized tool result must not reach the prompt.

MCP results were capped by MCP_MAX_RESPONSE_BYTES; webhook tools — the ones
customers actually write — returned whatever the endpoint sent, at any size.
A tool result stays in the context for the rest of the conversation, so it is
charged on every subsequent turn before the call dies on context length.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from turncall.domain.models import ToolDefinition
from turncall.services import tool_webhook
from turncall.services.tool_webhook import cap_tool_result, post_tool_webhook


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="d",
        parameters_schema={"type": "object", "properties": {}},
        webhook_url="https://customer.example/tool",
    )


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


@pytest.mark.unit
def test_a_result_within_the_limit_is_returned_verbatim() -> None:
    assert cap_tool_result("hello", 1024, tool="t", source="webhook") == "hello"


@pytest.mark.unit
def test_an_oversized_result_becomes_a_json_error_with_a_preview() -> None:
    payload = cap_tool_result("x" * 5000, 1000, tool="t", source="webhook")
    parsed = json.loads(payload)

    assert "5000 bytes" in parsed["error"]
    assert "limit 1000" in parsed["error"]
    # A preview the model can read, not the payload it was meant to replace.
    assert parsed["preview"] == "x" * 512
    assert len(payload.encode()) < 5000


@pytest.mark.unit
def test_the_preview_never_exceeds_a_limit_smaller_than_itself() -> None:
    parsed = json.loads(cap_tool_result("y" * 999, 64, tool="t", source="webhook"))
    assert parsed["preview"] == "y" * 64


@pytest.mark.unit
def test_a_multibyte_result_is_measured_in_bytes_and_cut_cleanly() -> None:
    """len() would undercount: 'é' is one character and two bytes. Cutting
    mid-character must not produce invalid text either."""
    text = "é" * 800  # 1600 bytes
    parsed = json.loads(cap_tool_result(text, 1000, tool="t", source="webhook"))

    assert "1600 bytes" in parsed["error"]
    parsed["preview"].encode()  # decoded cleanly, no partial character


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_tool_webhook_caps_what_the_endpoint_returned() -> None:
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Response("z" * 2_000_000))

    with patch.object(tool_webhook, "get_http_client", return_value=client):
        result = await post_tool_webhook(_tool(), {}, project_id=uuid.uuid4())

    assert json.loads(result)["error"].startswith("Tool result too large")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_ordinary_response_still_passes_through_untouched() -> None:
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Response('{"balance": 42}'))

    with patch.object(tool_webhook, "get_http_client", return_value=client):
        result = await post_tool_webhook(_tool(), {}, project_id=uuid.uuid4())

    assert result == '{"balance": 42}'
