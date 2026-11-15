"""A WhatsApp voice call opens like every inbound call (#494).

The connect is opened before pipecat's client answers it, through the same
module every carrier's edge opens through: the slot, the row with its
WhatsApp handles, `call.initializing` + `call.started`, and the agent the
number routes to (call-init's, else the bound one). What each case checks is what the
world holds afterwards: the row, the events, the slot, and the call running
sees.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, run_app
from tests.integration import test_call_init_project_scope_whatsapp as _scope
from tests.integration.call_init_scope_support import SCRIPT
from tests.integration.call_init_scope_support import (
    whatsapp_number as _call_init_whatsapp_number,
)
from tests.integration.test_live_call_limit import _whatsapp_connect
from tests.integration.test_session_message_rate import _enable_whatsapp
from tests.integration.test_text_session_variables import CallInitServer
from turncall.storage.database import get_session_factory
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration

# WhatsApp's WebRTC leg skipped, and the call running would see recorded.
whatsapp_voice = _scope.whatsapp_voice
_live_calls = _scope._live_calls


async def _whatsapp_number(app: Any, agent_id: str) -> str:
    number = await app.bind_number(agent_id)
    await _enable_whatsapp(number)
    return number


async def _row(call_id: uuid.UUID) -> Any:
    async with get_session_factory()() as session:
        return await call_repo.get_call_by_id(session, call_id)


async def test_a_connect_opens_its_call_with_its_whatsapp_handles(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        number = await _whatsapp_number(app, agent_id)
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        started = await app.sink.wait_for("call.started")
        initializing = app.sink.of("call.initializing")
        slots = await _live_calls(app.project_id)
        [(_, running)] = whatsapp_voice.started
        row = await _row(running.call_id)

    assert r.status_code == 200, r.text
    assert str(running.agent_id) == agent_id
    assert running.call_sid == "whatsapp:wa-call-1"
    assert started["call_id"] == str(running.call_id)
    assert started["payload"]["transport"] == "whatsapp"
    # As it always said (#487 changes no payload): the WhatsApp call's id.
    assert started["payload"]["whatsapp_call_id"] == "wa-call-1"
    assert [e["payload"]["transport"] for e in initializing] == ["whatsapp"]
    assert (row.from_number, row.to_number) == ("+15559876543", number)
    assert row.metadata_json["whatsapp_call_id"] == "wa-call-1"
    # Not the carrier's sid: a hang-up through the carrier would act on it.
    assert row.provider_call_sid is None
    assert slots == 1


async def test_a_weighted_number_still_runs_its_bound_agent(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    """WhatsApp voice runs call-init's agent, else the bound one: A/B
    weights are a phone call's, and opening it through the shared module
    changes nothing a WhatsApp caller gets."""
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        bound = await app.create_agent(HARNESS_AGENT)
        picked = await app.create_agent({**HARNESS_AGENT, "system_prompt": "B."})
        number = await _whatsapp_number(app, bound)
        numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
        number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
        r = await app.api.put(
            f"/v1/phone-numbers/{number_id}/routing",
            json={
                "weights": [
                    {"agent_id": bound, "weight": 1},
                    {"agent_id": picked, "weight": 99},
                ]
            },
        )
        assert r.status_code == 200, r.text
        await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        [(_, running)] = whatsapp_voice.started

    assert str(running.agent_id) == bound


async def test_a_connect_pipecat_cannot_answer_frees_its_slot_and_fails_its_call(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    from pipecat.transports.whatsapp.client import WhatsAppClient

    async def _broken(_self: Any, **_kwargs: Any) -> None:
        raise RuntimeError("WhatsApp refused the answer")

    monkeypatch.setattr(WhatsAppClient, "handle_webhook_request", _broken)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await _whatsapp_number(app, await app.create_agent(HARNESS_AGENT))
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        ended = await app.sink.wait_for("call.ended")
        slots = await _live_calls(app.project_id)

    assert r.status_code == 500
    assert ended["payload"]["status"] == "failed"
    assert slots == 0


async def test_an_inline_agents_call_started_names_no_agent(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    """An inline agent has no row and so no name: WhatsApp's `call.started`
    says null, as it always has."""
    inline = {**HARNESS_AGENT, "system_prompt": "An inline agent."}
    async with (
        CallInitServer({"agent": inline}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_whatsapp_number(app, server.url)
        await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        started = await app.sink.wait_for("call.started")

    assert started["agent_id"] is None
    assert started["payload"]["agent_id"] is None
    assert started["payload"]["agent_name"] is None
    assert started["payload"]["whatsapp_call_id"] == "wa-call-1"
