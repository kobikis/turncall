"""dispatch_event injects agent_id into call-event payloads and stamps event_id.

Covers ADR-0007: agent_id resolved centrally from the call's active_agent_id,
key always present (null when unresolved), and a single event_id minted per
logical event. The DB and HTTP boundaries are mocked — this is pure dispatch
logic.
"""

from uuid import uuid4

import pytest

from turncall.events import dispatcher
from turncall.events.webhook_delivery import DeliveryResult, WebhookEvent

pytestmark = pytest.mark.unit


def _patch(monkeypatch, *, active_agent_id):
    """Stub subscribers, the call lookup, and delivery; capture the event."""
    captured: dict = {}

    async def fake_subscribers(session, project_id, event_type):
        return [("https://example.test/hook", "secret")]

    class _Call:
        def __init__(self, agent_id):
            self.active_agent_id = agent_id

    async def fake_get_call(session, call_id, **kw):
        return _Call(active_agent_id)

    async def fake_deliver(event: WebhookEvent, subscribers):
        captured["event"] = event
        return [DeliveryResult(success=True, status_code=200, attempts=1)]

    monkeypatch.setattr(dispatcher, "get_active_subscribers", fake_subscribers)
    monkeypatch.setattr(dispatcher.call_repo, "get_call_by_id", fake_get_call)
    monkeypatch.setattr(dispatcher, "deliver_to_subscribers", fake_deliver)
    return captured


async def _drain() -> None:
    """Delivery now runs in a background task; run it so the mock captures."""
    import asyncio

    while dispatcher._BG_TASKS:
        await asyncio.gather(*list(dispatcher._BG_TASKS))


@pytest.mark.asyncio
async def test_injects_agent_id_and_event_id(monkeypatch):
    agent_id = uuid4()
    captured = _patch(monkeypatch, active_agent_id=agent_id)

    await dispatcher.dispatch_event(
        session=None,
        project_id=uuid4(),
        event_type="transcript.final",
        payload={"text": "hi"},
        call_id=uuid4(),
    )

    await _drain()
    event = captured["event"]
    assert event.agent_id == str(agent_id)  # envelope, not payload
    assert "agent_id" not in event.payload  # never injected into payload
    assert event.payload["text"] == "hi"  # original payload untouched
    assert event.event_id  # minted, not None


@pytest.mark.asyncio
async def test_agent_id_null_when_unresolved(monkeypatch):
    captured = _patch(monkeypatch, active_agent_id=None)

    await dispatcher.dispatch_event(
        session=None,
        project_id=uuid4(),
        event_type="call.initializing",
        payload={},
        call_id=uuid4(),
    )

    await _drain()
    # Envelope field present, value null when no agent resolved (ADR-0007).
    assert captured["event"].agent_id is None


@pytest.mark.asyncio
async def test_caller_supplied_agent_id_used_without_lookup(monkeypatch):
    supplied = uuid4()
    captured = _patch(monkeypatch, active_agent_id=uuid4())  # would differ if looked up

    await dispatcher.dispatch_event(
        session=None,
        project_id=uuid4(),
        event_type="chat.created",
        payload={},
        call_id=None,  # sms/chat path: no call lookup
        agent_id=supplied,
    )

    await _drain()
    assert captured["event"].agent_id == str(supplied)
    assert "agent_id" not in captured["event"].payload


@pytest.mark.asyncio
async def test_event_id_preserved_when_supplied(monkeypatch):
    captured = _patch(monkeypatch, active_agent_id=None)

    await dispatcher.dispatch_event(
        session=None,
        project_id=uuid4(),
        event_type="call.ended",
        payload={},
        call_id=uuid4(),
        event_id="explicit-id",
    )

    await _drain()
    assert captured["event"].event_id == "explicit-id"
