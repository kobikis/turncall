"""Event dispatcher — routes call events to webhook subscribers.

Connects the internal event system to external webhook delivery.
Called by the observability processor and call control service
whenever events are generated.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.events.webhook_delivery import WebhookEvent, deliver_to_subscribers
from turncall.storage.models import WebhookSubscriptionRow
from turncall.storage.repositories import call_repo

# Background delivery tasks kept referenced so asyncio doesn't GC them mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> None:  # type: ignore[no-untyped-def]
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _deliver_and_log(
    event: WebhookEvent,
    subscribers: list[tuple[str, str]],
    event_type: str,
) -> None:
    """Deliver in the background — no DB session touched — and log the summary."""
    results = await deliver_to_subscribers(event, subscribers)
    failed = sum(1 for r in results if not r.success)
    if failed:
        logger.warning(
            "webhook_delivery_partial_failure",
            event=event_type,
            delivered=len(results) - failed,
            failed=failed,
        )


async def get_active_subscribers(
    session: AsyncSession,
    project_id: UUID,
    event_type: str,
) -> list[tuple[str, str]]:
    """Get active webhook subscribers for a project and event type."""
    from sqlalchemy import select

    result = await session.execute(
        select(WebhookSubscriptionRow).where(
            WebhookSubscriptionRow.project_id == project_id,
            WebhookSubscriptionRow.active.is_(True),
        )
    )
    rows = result.scalars().all()

    subscribers: list[tuple[str, str]] = []
    for row in rows:
        # Check if subscription covers this event type
        subscribed_events = row.events or {}
        event_list = subscribed_events.get("events", [])
        if not event_list or event_type in event_list or "*" in event_list:
            subscribers.append((row.url, row.secret))

    return subscribers


async def dispatch_event(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    call_id: UUID | None = None,
    session_id: UUID | None = None,
    agent_id: UUID | None = None,
    event_id: str | None = None,
) -> int:
    """Dispatch an event to all matching webhook subscribers.

    Subscriber lookup runs on the passed session; actual HTTP delivery is handed
    to a background task so it never holds the DB connection. Returns the number
    of subscribers the event was queued for.
    """
    subscribers = await get_active_subscribers(session, project_id, event_type)
    if not subscribers:
        return 0

    # Resolve agent_id for the envelope (not the payload). A caller may pass it
    # (sms/chat); for call events we look it up from the call's current
    # active_agent_id so handoffs are reflected. Lookup runs only here — after
    # the no-subscriber early return — so silent calls cost nothing. See ADR-0007.
    resolved_agent_id = agent_id
    if resolved_agent_id is None and call_id is not None:
        call = await call_repo.get_call_by_id(session, call_id)
        resolved_agent_id = call.active_agent_id if call else None

    event = WebhookEvent(
        event_type=event_type,
        payload=payload,
        project_id=project_id,
        call_id=call_id,
        session_id=session_id,
        agent_id=str(resolved_agent_id) if resolved_agent_id else None,
        # One uuid per logical event: stable across retries, shared across
        # subscribers, so consumers can dedupe redeliveries. See ADR-0007.
        event_id=event_id or str(uuid4()),
    )

    # Deliver off the caller's DB session. get_active_subscribers + the agent
    # lookup above are the only DB work and they're done; HTTP delivery (up to
    # ~90s of retries against a dead subscriber) must not hold the caller's
    # connection or block the request/audio path (review finding #5). Callers
    # ignore the return, so it's now the count queued, not confirmed delivered.
    _spawn(_deliver_and_log(event, subscribers, event_type))
    return len(subscribers)
