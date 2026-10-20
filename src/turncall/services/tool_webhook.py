"""POST a custom webhook tool call and return the receiver's response.

Shared by both tool paths: the voice pipeline (orchestrator/tool_bridge) and
text sessions (services/chat_tools). They differ only in which identifier the
envelope carries — a call has a call_id, a chat session has a session_id — so
keeping one implementation keeps signing, timeouts and error text identical
whichever channel the customer's endpoint hears from.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.adapters.http_client import get_http_client
from turncall.config.settings import get_settings
from turncall.domain.models import ToolDefinition
from turncall.events.webhook_signing import sign_payload
from turncall.services import redaction
from turncall.services.outbound_url import warn_if_plain_http

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
        # Truthiness, not key presence. `{"error": null}` is the ordinary
        # "nothing went wrong" shape from an endpoint that always includes the
        # field, and reading it as a failure mislabelled every successful call
        # such an endpoint made.
        return ("failed" if parsed.get("error") else "succeeded"), parsed
    return "succeeded", {"result": parsed}


async def invocation_record(
    project_id: UUID,
    args: dict[str, Any],
    result: str,
    *,
    sessions: Callable[[], AsyncSession] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(status, input_json, output_json) as a `tool_invocations` row stores
    them, for both tool paths.

    For a project that redacts, the arguments and the result go through its
    redaction first (#314, ADR-0024): the row outlives the call and is served
    by the invocations reads. Their keys are the tool's own, so they are free
    JSON (`redaction.redact_json`), every string analysed whatever its key. The
    status is read from the raw result, so a redacted error still counts as
    one. Only the row is redacted: the webhook was sent the raw arguments and
    the model was handed the raw result, both before this runs.

    Waits on the detector: call it before the session's transaction begins
    (events/emit). The policy is read on a session of its own, from
    `sessions` (the app's factory by default).
    """
    status, output_json = classify_tool_result(result)
    policy = await redaction.policy_for_project(project_id, sessions=sessions)
    if policy is None:
        return status, args, output_json
    redacted_args, redacted_output = await redaction.redact_json(
        [args, output_json], policy
    )
    return status, redacted_args, redacted_output


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

    # An inline call-init tool's URL was never checked; the call still goes.
    try:
        warn_if_plain_http(
            "tool", tool_def.webhook_url, project_id=project_id, tool=tool_def.name
        )
    except Exception as exc:  # a log line must never cost the delivery
        logger.warning("plain_http_warning_failed", error=type(exc).__name__)

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
