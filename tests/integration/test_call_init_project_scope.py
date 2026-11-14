"""A call-init answer runs an agent of the number's own project only (#475).

An agent id is not a capability: it rides in webhooks, call reads and logs.
Through the running app, a call-init server answers another project's agent
id on every transport that runs call-init, and each transport treats it as it
treats a call-init answer naming no usable agent — the inbound call refused
at the carrier, WebRTC an error, WhatsApp voice rejected unanswered (#483),
an outbound dial falling back to its own agent, a text left unanswered. The
project's own agent id, on the same transports, runs as before.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web

from tests.harness import (
    HARNESS_AGENT,
    Script,
    answer_outbound_call,
    place_inbound_call,
    post_signed,
    run_app,
    telnyx,
)
from tests.integration.limits_support import ring
from tests.integration.test_live_call_limit import _whatsapp_connect
from tests.integration.test_session_message_rate import _enable_whatsapp, _whatsapp
from tests.integration.test_text_session_variables import CallInitServer, _sms
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

CALLER = "+15550009999"
OWN_PROMPT = "The number's own agent."
FOREIGN_PROMPT = "Another project's agent."
SCRIPT = Script(agent=("Hello there.",))


@pytest.fixture
def prompts(monkeypatch) -> list[str]:
    """Every system prompt an LLM was given: the text LLM's, stubbed here, and
    the voice one's once `_wrap_voice_llm` runs inside the app."""
    seen: list[str] = []

    async def _text(_config: Any, messages: list[dict], **_kwargs: Any) -> Any:
        seen.append(messages[0]["content"])
        return CompletionResult(text="Text reply.", total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _text)
    return seen


def _wrap_voice_llm(monkeypatch, seen: list[str]) -> list[str]:
    """Record each voice LLM's prompt. After `run_app`: the harness installs
    its stub LLM on entry, and this wraps it."""
    from turncall.orchestrator import pipeline_factory

    stub = pipeline_factory._create_llm_service

    def _voice(config: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(config.system_prompt)
        return stub(config, *args, **kwargs)

    monkeypatch.setattr(pipeline_factory, "_create_llm_service", _voice)
    return seen


async def _foreign_agent(app: Any, stack: Any) -> str:
    """An agent of another project on the same app."""
    _, other = await stack.enter_async_context(app.another_project())
    r = await other.post(
        "/v1/agents",
        json={
            "name": "foreign",
            "config": {**HARNESS_AGENT, "system_prompt": FOREIGN_PROMPT},
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["data"]["id"])


async def _route(app: Any, carrier: str, server_url: str, **extra: Any) -> str:
    """A fresh number on `carrier`, routed to the call-init `server_url`."""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    body: dict[str, Any] = {
        "e164_number": e164,
        "routing_target_type": "webhook",
        "server_url": server_url,
        **extra,
    }
    if carrier == "twilio":
        body["external_number_sid"] = app.carrier.add_number(e164).sid
    elif carrier == "telnyx":
        app.carrier.add_number(e164)
        body["carrier"] = "telnyx"
    else:
        await _give_the_project_an_asterisk(app)
        body["carrier"] = "asterisk"
    r = await app.api.post("/v1/phone-numbers", json=body)
    assert r.status_code == 201, r.text
    return e164


async def _give_the_project_an_asterisk(app: Any) -> None:
    await app.asterisk.start()
    stored = await app.api.put(
        "/v1/credentials/asterisk",
        json={
            "ari_url": app.asterisk.url,
            "username": app.asterisk.username,
            "password": app.asterisk.password,
        },
    )
    assert stored.status_code == 200, stored.text
    await asyncio.wait_for(app.asterisk.connected.wait(), 5)


async def _calls(app: Any) -> list[dict[str, Any]]:
    return (await app.api.get("/v1/calls")).json()["data"]


# -- inbound phone, every carrier --------------------------------------------


async def _ring_refused(app: Any, carrier: str, number: str) -> None:
    """Ring `number` and assert the carrier was told no, unanswered by any
    agent: Twilio's TwiML hangs up, Telnyx is hung up before `answer`,
    Asterisk's channel is continued to the dialplan."""
    if carrier == "twilio":
        r = await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).voice_url,
            ring(app.carrier, number),
        )
        assert r.status_code == 200, r.text
        assert "<Hangup" in r.text and "<Connect>" not in r.text
    elif carrier == "telnyx":
        cc_id = f"v3:{uuid.uuid4().hex}"
        initiated = telnyx.event(
            "call.initiated",
            {
                "call_control_id": cc_id,
                "connection_id": "harness-connection",
                "from": CALLER,
                "to": number,
                "direction": "incoming",
            },
        )
        r = await telnyx.post_signed(
            app.api, app.carrier, initiated, url=app.carrier.number(number).voice_url
        )
        assert r.status_code == 200, r.text
        assert [a.params["call_sid"] for a in app.carrier.actions_of("hang_up")] == [
            cc_id
        ]
        assert app.carrier.actions_of("answer") == []
    else:
        call = await app.asterisk.ring(CALLER, number, until_bridged=False)
        sent_back = await app.carrier.wait_for("continue")
        assert sent_back.params["call_sid"] == call.call_sid
        assert not call.call_id  # never admitted, never answered


async def _ring_answered(app: Any, carrier: str, number: str) -> Any:
    if carrier == "twilio":
        return await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
    if carrier == "telnyx":
        return await telnyx.place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
    return await app.ring_asterisk(CALLER, number)


CARRIERS = ["twilio", "telnyx", "asterisk"]


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_inbound_refuses_another_projects_agent(
    carrier: str, monkeypatch, prompts
) -> None:

    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        AsyncExitStack() as stack,
    ):
        seen = _wrap_voice_llm(monkeypatch, prompts)
        server.answer = {"agent_id": await _foreign_agent(app, stack)}
        number = await _route(app, carrier, server.url)
        await _ring_refused(app, carrier, number)
        calls = await _calls(app)

    assert len(server.requests) == 1
    assert calls == []
    assert FOREIGN_PROMPT not in seen


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_inbound_runs_the_projects_own_agent(
    carrier: str, monkeypatch, prompts
) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        seen = _wrap_voice_llm(monkeypatch, prompts)
        own = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        server.answer = {"agent_id": own}
        number = await _route(app, carrier, server.url)
        call = await _ring_answered(app, carrier, number)
        await call.wait_for_agent_speech(1)
        started = await app.sink.wait_for("call.started")
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    assert started["agent_id"] == own
    assert seen == [OWN_PROMPT]


# -- WebRTC ------------------------------------------------------------------


async def _connect(app: Any, server_url: str) -> Any:
    return await app.api.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "server_url": server_url},
    )


@pytest.fixture
def no_peer(monkeypatch) -> None:
    """The call is decided before the peer is negotiated; a real aiortc peer
    left behind would keep the test process alive."""
    from turncall.api.v1 import webrtc

    async def _no_peer(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("no peer in this test")

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", _no_peer)


async def test_webrtc_refuses_another_projects_agent(monkeypatch, no_peer) -> None:

    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        AsyncExitStack() as stack,
    ):
        server.answer = {"agent_id": await _foreign_agent(app, stack)}
        r = await _connect(app, server.url)
        calls = await _calls(app)

    assert len(server.requests) == 1
    assert r.status_code == 502, r.text
    assert r.json()["success"] is False
    assert calls == []


async def test_webrtc_runs_the_projects_own_agent(monkeypatch, no_peer) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        own = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        server.answer = {"agent_id": own}
        await _connect(app, server.url)
        started = await app.sink.wait_for("call.started")

    assert started["agent_id"] == own


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

    async def _start(*, agent: Any, call_context: Any, **_kwargs: Any) -> None:
        leg.started.append((agent.config, call_context))

    monkeypatch.setattr(WhatsAppClient, "handle_webhook_request", _connected)
    monkeypatch.setattr(WhatsAppApi, "reject_call_to_whatsapp", _reject)
    monkeypatch.setattr(
        whatsapp_handlers, "create_whatsapp_transport", lambda *a, **k: None
    )
    monkeypatch.setattr(whatsapp_handlers, "start_call_pipeline", _start)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    return leg


async def _whatsapp_number(app: Any, server_url: str) -> str:
    number = await _route(app, "twilio", server_url)
    await _enable_whatsapp(number)
    return number


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


# -- outbound dial -----------------------------------------------------------


async def _dial(app: Any, number: str, agent_id: str) -> str:
    numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
    number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
    r = await app.api.post(
        "/v1/calls/outbound",
        json={
            "agent_id": agent_id,
            "from_number_id": number_id,
            "to_number": "+15550007777",
        },
    )
    assert r.status_code == 201, r.text
    dial = await app.carrier.wait_for("dial")
    call = await answer_outbound_call(app.carrier, app.api, dial)
    await call.wait_for_agent_speech(1)
    await call.hang_up()
    await app.sink.wait_for("call.ended")
    return str(r.json()["data"]["id"])


async def test_outbound_falls_back_to_the_dials_agent(monkeypatch, prompts) -> None:

    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        AsyncExitStack() as stack,
    ):
        seen = _wrap_voice_llm(monkeypatch, prompts)
        server.answer = {"agent_id": await _foreign_agent(app, stack)}
        dialled = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        number = await _route(app, "twilio", server.url)
        call_id = await _dial(app, number, dialled)
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]

    assert len(server.requests) == 1
    assert read["active_agent_id"] == dialled
    assert seen == [OWN_PROMPT]


async def test_outbound_runs_the_projects_own_agent(monkeypatch, prompts) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        seen = _wrap_voice_llm(monkeypatch, prompts)
        dialled = await app.create_agent({**HARNESS_AGENT, "system_prompt": "Dialled."})
        own = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        server.answer = {"agent_id": own}
        number = await _route(app, "twilio", server.url)
        call_id = await _dial(app, number, dialled)
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]

    assert read["active_agent_id"] == own
    assert seen == [OWN_PROMPT]


# -- SMS and WhatsApp text ---------------------------------------------------


async def _text(app: Any, channel: str, server_url: str) -> Any:
    if channel == "sms":
        number = await _route(app, "twilio", server_url, sms_enabled=True)
        return await _sms(app, number, "SM1", "hi")
    number = await _whatsapp_number(app, server_url)
    return await app.api.post("/webhooks/whatsapp", json=_whatsapp(number, "hi"))


@pytest.fixture
def whatsapp_sent(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def _send(_http: Any, *, text: str, **_kwargs: Any) -> dict:
        sent.append(text)
        return {}

    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    return sent


@pytest.mark.parametrize("channel", ["sms", "whatsapp"])
async def test_text_leaves_another_projects_agent_unanswered(
    channel: str, monkeypatch, prompts, whatsapp_sent
) -> None:

    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        AsyncExitStack() as stack,
    ):
        server.answer = {"agent_id": await _foreign_agent(app, stack)}
        r = await _text(app, channel, server.url)
        sessions = (await app.api.get("/v1/chat/sessions")).json()["data"]

    seen = prompts
    assert r.status_code == 200
    assert len(server.requests) == 1
    assert "Text reply." not in r.text and whatsapp_sent == []
    assert seen == []
    assert sessions == []


@pytest.mark.parametrize("channel", ["sms", "whatsapp"])
async def test_text_runs_the_projects_own_agent(
    channel: str, monkeypatch, prompts, whatsapp_sent
) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        own = await app.create_agent({**HARNESS_AGENT, "system_prompt": OWN_PROMPT})
        server.answer = {"agent_id": own}
        await _text(app, channel, server.url)
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]

    seen = prompts
    assert seen == [OWN_PROMPT]
    assert session["agent_id"] == own
