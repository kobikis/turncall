"""Server-side event hooks.

Unlike fire-and-forget webhooks, server events are request/response:
the platform POSTs an event to the developer's server URL and the
response can influence call behavior (e.g., return agent config
dynamically, handle tool calls, etc.).

Supported events:
  - call-init: Before agent selection. Response can override agent config.
  - function-call: Tool execution delegated to server. Response is the tool result.
  - call-end: After call ends. Includes transcript, summary, duration.
  - status-update: Call status changes (started, ended, transferred, etc.).
  - speech-update: Real-time speech events (started/stopped speaking).
  - transcript-update: Transcript updates (partial and final).
  - hang: Notifies server of voicemail/silence detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.events.webhook_signing import sign_payload


class ServerEventType(StrEnum):
    CALL_INIT = "call-init"
    FUNCTION_CALL = "function-call"
    CALL_END = "call-end"
    STATUS_UPDATE = "status-update"
    SPEECH_UPDATE = "speech-update"
    TRANSCRIPT_UPDATE = "transcript-update"
    HANG = "hang"


@dataclass(frozen=True)
class ServerEventRequest:
    """Outbound request to the developer's server."""

    event_type: ServerEventType
    call_id: str
    payload: dict[str, Any]
    timestamp: str | None = None


@dataclass(frozen=True)
class ServerEventResponse:
    """Response from the developer's server."""

    success: bool
    status_code: int
    data: dict[str, Any] | None = None
    error: str | None = None


async def send_server_event(
    server_url: str,
    event: ServerEventRequest,
    *,
    secret: str | None = None,
    timeout_seconds: float = 5.0,
) -> ServerEventResponse:
    """Send a server event and return the response.

    Unlike webhooks, this is synchronous — we wait for the response
    because it may influence call behavior.
    """
    body_dict: dict[str, Any] = {
        "message": {
            "type": event.event_type.value,
            "call": {"id": event.call_id},
            "timestamp": event.timestamp or datetime.now(UTC).isoformat(),
            **event.payload,
        },
    }

    import json

    body = json.dumps(body_dict)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        signature, timestamp = sign_payload(body, secret)
        headers["X-TurnCall-Signature"] = signature
        headers["X-TurnCall-Timestamp"] = str(timestamp)

    client = get_http_client()
    try:
        response = await client.post(
            server_url,
            content=body,
            headers=headers,
            timeout=timeout_seconds,
        )

        response_data = None
        if response.headers.get("content-type", "").startswith("application/json"):
            response_data = response.json()

        if response.status_code >= 400:
            logger.warning(
                "server_event_error_response",
                event=event.event_type,
                status=response.status_code,
                url=server_url,
            )
            return ServerEventResponse(
                success=False,
                status_code=response.status_code,
                data=response_data,
                error=f"Server returned {response.status_code}",
            )

        return ServerEventResponse(
            success=True,
            status_code=response.status_code,
            data=response_data,
        )

    # Broad: call-init sits on the inbound-call path, so a malformed URL or any
    # transport error must degrade to "no dynamic agent", never crash the call.
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning(
            "server_event_error",
            event=event.event_type,
            url=server_url,
            error=str(exc),
        )
        return ServerEventResponse(
            success=False,
            status_code=0,
            error=f"Server event failed: {exc}",
        )


async def send_call_init(
    server_url: str,
    *,
    call_id: str,
    from_number: str,
    to_number: str,
    call_sid: str | None = None,
    transport_type: str = "inboundPhoneCall",
    provider_call_id: str | None = None,
    secret: str | None = None,
    timeout_seconds: float = 5.0,
) -> ServerEventResponse:
    """Send call-init event. Response should contain agent config.

    This is called before agent selection on inbound calls when
    the phone number is configured with a server URL instead of a
    static agent ID.

    Args:
        transport_type: Call type identifier (e.g., "inboundPhoneCall",
            "inboundWhatsAppCall", "webrtc").
        provider_call_id: External provider call ID. Falls back to call_sid
            for backward compatibility.
    """
    resolved_provider_id = provider_call_id or call_sid or ""
    event = ServerEventRequest(
        event_type=ServerEventType.CALL_INIT,
        call_id=call_id,
        payload={
            "phoneNumber": {"number": to_number},
            "customer": {"number": from_number},
            "call": {
                "id": call_id,
                "provider_call_id": resolved_provider_id,
                "type": transport_type,
            },
        },
    )
    return await send_server_event(
        server_url,
        event,
        secret=secret,
        timeout_seconds=timeout_seconds,
    )


async def send_call_end(
    server_url: str,
    *,
    call_id: str,
    transcript: list[dict[str, str]],
    summary: str | None = None,
    duration_ms: int | None = None,
    ended_reason: str = "completed",
    secret: str | None = None,
) -> ServerEventResponse:
    """Send call-end report with full transcript and metadata."""
    event = ServerEventRequest(
        event_type=ServerEventType.CALL_END,
        call_id=call_id,
        payload={
            "endedReason": ended_reason,
            "transcript": transcript,
            "summary": summary,
            "durationMs": duration_ms,
        },
    )
    return await send_server_event(
        server_url,
        event,
        secret=secret,
        timeout_seconds=10.0,
    )


async def send_status_update(
    server_url: str,
    *,
    call_id: str,
    status: str,
    secret: str | None = None,
) -> ServerEventResponse:
    """Send a call status update event."""
    event = ServerEventRequest(
        event_type=ServerEventType.STATUS_UPDATE,
        call_id=call_id,
        payload={"status": status},
    )
    return await send_server_event(server_url, event, secret=secret)


async def send_transcript_event(
    server_url: str,
    *,
    call_id: str,
    role: str,
    text: str,
    is_final: bool = True,
    secret: str | None = None,
) -> ServerEventResponse:
    """Send a transcript update event."""
    event = ServerEventRequest(
        event_type=ServerEventType.TRANSCRIPT_UPDATE,
        call_id=call_id,
        payload={
            "role": role,
            "transcript": text,
            "transcriptType": "final" if is_final else "partial",
        },
    )
    return await send_server_event(server_url, event, secret=secret)


async def send_function_call_event(
    server_url: str,
    *,
    call_id: str,
    function_name: str,
    arguments: dict[str, Any],
    secret: str | None = None,
    timeout_seconds: float = 10.0,
) -> ServerEventResponse:
    """Send a function-call event. Response should contain the result.

    This allows the developer's server to handle tool execution
    instead of TurnCall making webhook calls per-tool.
    """
    event = ServerEventRequest(
        event_type=ServerEventType.FUNCTION_CALL,
        call_id=call_id,
        payload={
            "functionCall": {
                "name": function_name,
                "parameters": arguments,
            },
        },
    )
    return await send_server_event(
        server_url,
        event,
        secret=secret,
        timeout_seconds=timeout_seconds,
    )
