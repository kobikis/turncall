"""An outbound dial starts its call the way an inbound call does (#495).

The dial keeps its own opener — suppression, limits, call-init at the dial,
the carrier request — but its row and its `call.initializing` +
`call.started` are the opening module's write. So for the same agent, the
row a dial leaves and the start events a subscriber hears have the shape an
inbound call's have: the agent on the row (none for an inline agent, whose
raw config is kept, ADR-0017), and the same fields in each start event.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.test_text_session_variables import CallInitServer
from turncall.domain.carriers import TWILIO
from turncall.services.call_opening import (
    INBOUND_PHONE_CALL,
    CallParties,
    EdgeHandles,
    NumberSource,
    OpenedCall,
    open_call,
)
from turncall.storage.database import get_session_factory
from turncall.storage.repositories import call_repo, phone_number_repo

pytestmark = pytest.mark.integration

CALLER = "+15550004444"
CALLEE = "+15550005555"
INLINE = {**HARNESS_AGENT, "system_prompt": "An inline agent for {{name}}."}
START_EVENTS = ("call.initializing", "call.started")


async def _number(app: Any, **routing: Any) -> tuple[str, str]:
    """A number on the fake carrier routed by `routing`. (e164, id)"""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": app.carrier.add_number(e164).sid,
            **routing,
        },
    )
    assert r.status_code == 201, r.text
    return e164, r.json()["data"]["id"]


async def _inbound(e164: str) -> UUID:
    async with get_session_factory()() as session:
        number = await phone_number_repo.get_by_e164(session, e164)
        assert number is not None
        opened = await open_call(
            NumberSource(
                number=number,
                parties=CallParties(from_number=CALLER, to_number=e164),
                handles=EdgeHandles(
                    carrier=TWILIO, provider_call_sid=f"CA{uuid.uuid4().hex}"
                ),
                call_type=INBOUND_PHONE_CALL,
                weighted=True,
            ),
            project_id=number.project_id,
            session=session,
        )
    assert isinstance(opened, OpenedCall), opened
    return opened.call_id


async def _outbound(app: Any, agent_id: str, number_id: str) -> UUID:
    r = await app.api.post(
        "/v1/calls/outbound",
        json={"agent_id": agent_id, "from_number_id": number_id, "to_number": CALLEE},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["data"]["id"])


async def _started(app: Any, call_id: UUID) -> dict[str, Any]:
    """The call's row and its start events' payloads, by type."""
    async with get_session_factory()() as session:
        row = await call_repo.get_call_by_id(session, call_id)
    r = await app.api.get(f"/v1/calls/{call_id}/events")
    assert r.status_code == 200, r.text
    events = {
        e["event_type"]: e["payload"]
        for e in r.json()["data"]
        if e["event_type"] in START_EVENTS
    }
    return {"row": row, "events": events}


def _shape(started: dict[str, Any]) -> dict[str, Any]:
    """What must agree between an inbound and an outbound call: everything
    but the parties, the direction and the carrier's id for the call."""
    row, events = started["row"], started["events"]
    started_payload = events["call.started"]
    return {
        "status": row.status,
        "carrier": row.provider,
        "active_agent_id": row.active_agent_id,
        "dynamic_config": row.metadata_json.get("dynamic_config"),
        "template_variables": row.metadata_json.get("template_variables"),
        "events": sorted(events),
        "fields": {kind: sorted(payload) for kind, payload in events.items()},
        "agent_name": started_payload["agent_name"],
        "inline": started_payload["dynamic_config"],
        "has_sid": bool(row.provider_call_sid and started_payload["provider_call_sid"]),
    }


async def test_a_stored_agents_dial_starts_like_an_inbound_call(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        e164, number_id = await _number(
            app, routing_target_type="agent", routing_target_id=agent_id
        )
        inbound = await _started(app, await _inbound(e164))
        outbound = await _started(app, await _outbound(app, agent_id, number_id))

    assert _shape(outbound) == _shape(inbound)
    assert str(outbound["row"].active_agent_id) == agent_id
    assert outbound["events"]["call.started"]["agent_name"] != "dynamic"
    assert outbound["events"]["call.initializing"]["direction"] == "outbound"
    # A field a subscriber already read is kept (#487: no API change).
    assert outbound["events"]["call.started"]["agent_id"] == agent_id


async def test_an_inline_agents_dial_starts_like_an_inbound_call(monkeypatch) -> None:
    async with (
        CallInitServer({"agent": INLINE, "variables": {"name": "Jane"}}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        dialled_as = await app.create_agent(HARNESS_AGENT)
        e164, number_id = await _number(
            app, routing_target_type="webhook", server_url=server.url
        )
        inbound = await _started(app, await _inbound(e164))
        outbound = await _started(app, await _outbound(app, dialled_as, number_id))

    assert _shape(outbound) == _shape(inbound)
    assert outbound["row"].active_agent_id is None
    assert outbound["row"].metadata_json["dynamic_config"] == INLINE
    assert outbound["events"]["call.started"]["dynamic_config"] is True
    assert outbound["events"]["call.started"]["agent_id"] is None
