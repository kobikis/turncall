"""A WhatsApp voice call runs an agent of the number's own project only
(#475), and one it cannot run is rejected unanswered (#483): another
project's agent id, a call-init naming none, a bound agent that is gone, a
number nobody bound. The project's own agent, and a number's bound one, run.
"""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web

from tests.harness import HARNESS_AGENT, run_app
from tests.integration.call_init_scope_support import OWN_PROMPT, SCRIPT
from tests.integration.call_init_scope_support import calls as _calls
from tests.integration.call_init_scope_support import (
    foreign_agent as _foreign_agent,
)
from tests.integration.call_init_scope_support import (
    whatsapp_number as _whatsapp_number,
)
from tests.integration.test_live_call_limit import _whatsapp_connect
from tests.integration.test_session_message_rate import _enable_whatsapp
from tests.integration.test_text_session_variables import CallInitServer

pytestmark = pytest.mark.integration


# -- WhatsApp voice ----------------------------------------------------------


@dataclass
class WhatsAppLeg:
    """What became of WhatsApp voice connects: `accepted` the connects handed
    to pipecat's client (which pre-accepts and accepts them), `rejected` the
    WhatsApp call ids refused unanswered, `started` each pipeline's
    (config, call context)."""

    accepted: list[Any] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    started: list[Any] = field(default_factory=list)


@pytest.fixture
def whatsapp_voice(monkeypatch) -> WhatsAppLeg:
    """WhatsApp's WebRTC leg, skipped: pipecat's client hands the call's
    connection straight to TurnCall, and the pipeline it would start is
    recorded instead of run."""
    from pipecat.transports.whatsapp.api import WhatsAppApi
    from pipecat.transports.whatsapp.client import WhatsAppClient

    from turncall.webhooks import whatsapp_handlers

    leg = WhatsAppLeg()

    async def _connected(_self: Any, *, request: Any, connection_callback: Any) -> None:
        leg.accepted.append(request)
        await connection_callback(object())

    async def _reject(_self: Any, call_id: str) -> dict:
        leg.rejected.append(call_id)
        return {"success": True}

    async def _start(opened: Any, *_args: Any, **_kwargs: Any) -> None:
        # The call as running sees it: its agent, id and agent id.
        leg.started.append((opened.agent.config, opened))

    monkeypatch.setattr(WhatsAppClient, "handle_webhook_request", _connected)
    monkeypatch.setattr(WhatsAppApi, "reject_call_to_whatsapp", _reject)
    monkeypatch.setattr(
        whatsapp_handlers, "create_whatsapp_transport", lambda *a, **k: None
    )
    monkeypatch.setattr(whatsapp_handlers, "run_call", _start)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    return leg


async def _live_calls(project_id: str) -> int:
    from turncall.services import limits
    from turncall.storage.redis import get_redis

    return await get_redis().zcard(limits._live_calls_key(uuid.UUID(project_id)))


async def _connect_whatsapp(app: Any, number: str) -> tuple[Any, int, list]:
    """A WhatsApp voice connect to `number`: the webhook's response, the
    project's live-call slots after it and the calls it holds."""
    r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
    return r, await _live_calls(app.project_id), await _calls(app)


def assert_whatsapp_rejected(leg: WhatsAppLeg, outcome: tuple[Any, int, list]) -> None:
    """Refused unanswered (#483): rejected, never accepted, no pipeline, no
    call row, the slot freed."""
    r, slots, calls = outcome
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "rejected"}
    assert leg.rejected == ["wa-call-1"]
    assert leg.accepted == []
    assert leg.started == []
    assert slots == 0
    assert calls == []


class _FailingCallInit(CallInitServer):
    """A call-init server answering 500."""

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        return web.Response(status=500)


async def test_whatsapp_voice_rejects_another_projects_agent(
    monkeypatch, redis_env, whatsapp_voice
) -> None:

    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        AsyncExitStack() as stack,
    ):
        server.answer = {"agent_id": await _foreign_agent(app, stack)}
        number = await _whatsapp_number(app, server.url)
        outcome = await _connect_whatsapp(app, number)

    assert len(server.requests) == 1
    assert_whatsapp_rejected(whatsapp_voice, outcome)


@pytest.mark.parametrize("server_type", [CallInitServer, _FailingCallInit])
async def test_whatsapp_voice_rejects_a_call_init_naming_no_agent(
    server_type: type[CallInitServer], monkeypatch, redis_env, whatsapp_voice
) -> None:
    """An answer naming no agent, and a call-init that fails."""
    async with (
        server_type({"variables": {"name": "Jane"}}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _whatsapp_number(app, server.url)
        outcome = await _connect_whatsapp(app, number)

    assert len(server.requests) == 1
    assert_whatsapp_rejected(whatsapp_voice, outcome)


async def test_whatsapp_voice_rejects_a_number_whose_agent_is_gone(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await _whatsapp_number_routed_to_a_missing_agent(app)
        outcome = await _connect_whatsapp(app, number)

    assert_whatsapp_rejected(whatsapp_voice, outcome)


async def _whatsapp_number_routed_to_a_missing_agent(app: Any) -> str:
    """A WhatsApp number bound to an agent, then pointed at an id no agent row
    has: the column carries no foreign key."""
    from sqlalchemy import update

    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory
    from turncall.storage.models import PhoneNumberRow

    e164 = await app.bind_number(await app.create_agent(HARNESS_AGENT))
    await _enable_whatsapp(e164)
    engine = create_engine(Settings().database)
    async with create_session_factory(engine)() as session:
        await session.execute(
            update(PhoneNumberRow)
            .where(PhoneNumberRow.e164_number == e164)
            .values(routing_target_id=uuid.uuid4())
        )
        await session.commit()
    await engine.dispose()
    return e164


async def test_whatsapp_voice_rejects_a_number_nobody_bound(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    """No phone-number row: no agent and no slot, rejected all the same."""
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        outcome = await _connect_whatsapp(app, "+1555" + str(uuid.uuid4().int)[:7])

    assert_whatsapp_rejected(whatsapp_voice, outcome)


async def test_whatsapp_voice_runs_the_projects_own_agent(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        own = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        server.answer = {"agent_id": own}
        number = await _whatsapp_number(app, server.url)
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        calls = await _calls(app)

    assert r.json() == {"status": "ok"}
    assert len(server.requests) == 1  # asked once, before the answer
    assert len(whatsapp_voice.accepted) == 1
    assert whatsapp_voice.rejected == []
    [(config, context)] = whatsapp_voice.started
    assert config.system_prompt == OWN_PROMPT
    assert str(context.agent_id) == own
    assert [c["id"] for c in calls] == [str(context.call_id)]


async def test_whatsapp_voice_runs_a_numbers_bound_agent(
    monkeypatch, redis_env, whatsapp_voice
) -> None:
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        bound = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        e164 = await app.bind_number(bound)
        await _enable_whatsapp(e164)
        await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(e164))
        started = await app.sink.wait_for("call.started")

    assert whatsapp_voice.rejected == []
    [(config, context)] = whatsapp_voice.started
    assert config.system_prompt == OWN_PROMPT
    assert str(context.agent_id) == bound
    assert started["payload"]["agent_name"] == "harness"
