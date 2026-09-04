"""Webhook delivery with retry.

Delivers signed webhook events to subscriber URLs with exponential-backoff
retry. A delivery that exhausts its retries returns a failed DeliveryResult
(logged); there is no dead-letter store or manual-retry queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.events.webhook_signing import sign_payload

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class WebhookEvent:
    """An event to be delivered to webhook subscribers."""

    event_type: str
    payload: dict[str, Any]
    project_id: UUID
    call_id: UUID | None = None
    session_id: UUID | None = None
    agent_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """Result of a webhook delivery attempt."""

    success: bool
    status_code: int | None = None
    attempts: int = 0
    error: str | None = None


async def deliver_webhook(
    url: str,
    event: WebhookEvent,
    secret: str,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_SECONDS,
) -> DeliveryResult:
    """Deliver a webhook event with retry and exponential backoff.

    Signs the payload with HMAC-SHA256 and includes the signature
    in the X-TurnCall-Signature header.
    """
    body = json.dumps(
        {
            "event": event.event_type,
            "payload": event.payload,
            "project_id": str(event.project_id),
            "call_id": str(event.call_id) if event.call_id else None,
            "session_id": str(event.session_id) if event.session_id else None,
            "agent_id": event.agent_id,
            "event_id": event.event_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )

    signature, timestamp = sign_payload(body, secret)

    headers = {
        "Content-Type": "application/json",
        "X-TurnCall-Signature": signature,
        "X-TurnCall-Timestamp": str(timestamp),
        "X-TurnCall-Event": event.event_type,
    }

    import asyncio

    client = get_http_client()
    for attempt in range(max_retries + 1):
        try:
            response = await client.post(
                url,
                content=body,
                headers=headers,
                timeout=10.0,
            )
            if response.status_code < 400:
                return DeliveryResult(
                    success=True,
                    status_code=response.status_code,
                    attempts=attempt + 1,
                )

            logger.warning(
                "webhook_delivery_failed",
                url=url,
                status=response.status_code,
                attempt=attempt + 1,
                event=event.event_type,
            )
        # Broad on purpose: a subscriber's malformed URL (InvalidURL) or a
        # ReadError/RemoteProtocolError is user-controlled input and must not
        # escape to abort delivery to the other subscribers.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "webhook_delivery_error",
                url=url,
                error=str(exc),
                attempt=attempt + 1,
                event=event.event_type,
            )

        if attempt < max_retries:
            delay = base_delay * (2**attempt)
            await asyncio.sleep(delay)

    return DeliveryResult(
        success=False,
        attempts=max_retries + 1,
        error="Max retries exceeded",
    )


async def deliver_to_subscribers(
    event: WebhookEvent,
    subscribers: list[tuple[str, str]],
) -> list[DeliveryResult]:
    """Deliver a webhook event to all subscribers.

    Args:
        event: The webhook event to deliver.
        subscribers: List of (url, secret) tuples.

    Returns:
        List of delivery results, one per subscriber.
    """
    import asyncio

    tasks = [deliver_webhook(url, event, secret) for url, secret in subscribers]
    # return_exceptions so one subscriber's unexpected failure can't cancel
    # delivery to the rest; deliver_webhook already returns a failed result for
    # known errors, so an exception here means a genuine bug — logged, not raised.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[DeliveryResult] = []
    for (url, _), r in zip(subscribers, results, strict=True):
        if isinstance(r, DeliveryResult):
            out.append(r)
        else:
            logger.opt(exception=r).error(
                "webhook_delivery_unexpected for {url}", url=url
            )
            out.append(
                DeliveryResult(success=False, attempts=0, error=str(r))
            )
    return out
