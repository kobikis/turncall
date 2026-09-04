"""Review finding #5: dispatch_event must not await HTTP delivery — it returns
after the fast DB reads and hands delivery to a background task, so a slow/dead
subscriber never holds the caller's DB connection."""

import asyncio
from uuid import uuid4

import pytest

from turncall.events import dispatcher
from turncall.events.webhook_delivery import DeliveryResult


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivery_runs_in_background(monkeypatch):
    order: list[str] = []
    release = asyncio.Event()

    async def fake_subscribers(session, project_id, event_type):
        return [("https://x/hook", "s")]

    async def slow_deliver(event, subscribers):
        order.append("deliver_start")
        await release.wait()
        order.append("deliver_done")
        return [DeliveryResult(success=True, status_code=200, attempts=1)]

    monkeypatch.setattr(dispatcher, "get_active_subscribers", fake_subscribers)
    monkeypatch.setattr(dispatcher, "deliver_to_subscribers", slow_deliver)

    n = await dispatcher.dispatch_event(
        session=None,
        project_id=uuid4(),
        event_type="chat.created",
        payload={},
        agent_id=uuid4(),
    )
    order.append("returned")
    await asyncio.sleep(0)  # let the background task start

    # dispatch_event returned (subscriber count) while delivery is still blocked.
    assert n == 1
    assert order == ["returned", "deliver_start"], order

    release.set()
    while dispatcher._BG_TASKS:
        await asyncio.gather(*list(dispatcher._BG_TASKS))
    assert "deliver_done" in order
