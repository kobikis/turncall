"""POST a custom webhook tool call and return the receiver's response.

Shared by both tool paths: the voice pipeline (orchestrator/tool_bridge) and
text sessions (services/chat_tools). They differ only in which identifier the
envelope carries — a call has a call_id, a chat session has a session_id — so
keeping one implementation keeps signing, timeouts and error text identical
whichever channel the customer's endpoint hears from.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.config.settings import get_settings
from turncall.domain.models import ToolDefinition
from turncall.events.webhook_signing import sign_payload

# How much of an oversized result to show the model. Enough to see what the
# tool was answering, small enough that the cap still means something.
_PREVIEW_BYTES = 512


def cap_tool_result(text: str, max_bytes: int, *, tool: str, source: str) -> str:
    """Keep a runaway tool result out of the LLM context.

    Shared by both kinds of tool: the limit is the caller's (MCP servers and
    webhook tools have separate knobs), the behaviour is not. The reply stays
    valid JSON so the model can read the error and ask for less instead of
    losing the turn.
    """
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text

    logger.warning(
        "tool_response_truncated",
        tool=tool,
        source=source,
        size=len(encoded),
        max=max_bytes,
    )
    preview = encoded[: min(_PREVIEW_BYTES, max_bytes)].decode(errors="ignore")
    return json.dumps(
        {
            "error": (
                f"Tool result too large: {len(encoded)} bytes, "
                f"limit {max_bytes}. Ask for less data."
            ),
            "preview": preview,
        }
    )


def classify_tool_result(result: str) -> tuple[str, dict[str, Any]]:
    """Turn a tool's raw string result into (status, output_json).

    Every executor hands back a string — JSON when the endpoint sent JSON,
    whatever it sent otherwise — so the status has to be read back out of
    it. The error shape is ours: post_tool_webhook and the MCP client both
    report failures as {"error": ...}.
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return "succeeded", {"result": result}
    if isinstance(parsed, dict):
        return ("failed" if "error" in parsed else "succeeded"), parsed
    return "succeeded", {"result": parsed}


async def post_tool_webhook(
    tool_def: ToolDefinition,
    args: dict[str, Any],
    *,
    project_id: UUID,
    call_id: UUID | None = None,
    session_id: UUID | None = None,
) -> str:
    """Invoke a webhook tool. Never raises — a failure is returned as JSON text
    so the model can tell the customer something went wrong and carry on."""
    if not tool_def.webhook_url:
        return '{"error": "No webhook URL configured"}'

    payload = {
        "tool_name": tool_def.name,
        "arguments": args,
        "project_id": str(project_id),
        # Exactly one of these is set: voice calls have no session, text
        # sessions have no call.
        "call_id": str(call_id) if call_id else None,
        "session_id": str(session_id) if session_id else None,
    }
    # Sign over the exact bytes sent so the receiver can verify them verbatim.
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if tool_def.webhook_secret:
        signature, ts = sign_payload(body, tool_def.webhook_secret)
        headers["X-TurnCall-Signature"] = signature
        headers["X-TurnCall-Timestamp"] = str(ts)

    client = get_http_client()
    try:
        response = await client.post(
            tool_def.webhook_url,
            content=body,
            headers=headers,
            timeout=tool_def.timeout_seconds,
        )
        response.raise_for_status()
        return cap_tool_result(
            response.text,
            get_settings().tools.max_response_bytes,
            tool=tool_def.name,
            source="webhook",
        )
    except httpx.TimeoutException:
        logger.warning(
            "tool_webhook_timeout",
            tool=tool_def.name,
            timeout=tool_def.timeout_seconds,
        )
        return '{"error": "Tool execution timed out"}'
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "tool_webhook_error",
            tool=tool_def.name,
            status=exc.response.status_code,
        )
        return f'{{"error": "Tool returned status {exc.response.status_code}"}}'
    except httpx.HTTPError as exc:
        # DNS, TLS, connection refused. Previously this propagated into the
        # pipeline; a text reply can't afford to lose a turn over it.
        logger.warning("tool_webhook_failed", tool=tool_def.name, error=str(exc))
        return '{"error": "Tool request failed"}'
