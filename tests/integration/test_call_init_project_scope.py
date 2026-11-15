"""A call-init answer runs an agent of the number's own project only (#475).

An agent id is not a capability: it rides in webhooks, call reads and logs.
Through the running app, a call-init server answers another project's agent
id on every transport that runs call-init, and each transport treats it as it
treats a call-init answer naming no usable agent — the inbound call refused
at the carrier, WebRTC an error, WhatsApp voice rejected unanswered (#483,
`test_call_init_project_scope_whatsapp.py`), an outbound dial falling back to its own agent, a text left unanswered. The
project's own agent id, on the same transports, runs as before.
"""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from typing import Any

import pytest

from tests.harness import (
    HARNESS_AGENT,
    answer_outbound_call,
    place_inbound_call,
    post_signed,
    run_app,
    telnyx,
)
from tests.integration.call_init_scope_support import (
    CALLER,
    FOREIGN_PROMPT,
    OWN_PROMPT,
    SCRIPT,
)
from tests.integration.call_init_scope_support import calls as _calls
from tests.integration.call_init_scope_support import (
    foreign_agent as _foreign_agent,
)
from tests.integration.call_init_scope_support import route as _route
from tests.integration.call_init_scope_support import (
    whatsapp_number as _whatsapp_number,
)
from tests.integration.limits_support import ring
from tests.integration.test_session_message_rate import _whatsapp
from tests.integration.test_text_session_variables import CallInitServer, _sms
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration


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
