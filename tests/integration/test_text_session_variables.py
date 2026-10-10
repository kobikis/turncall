"""`{{variables}}` reach a text conversation: Chat API, SMS and WhatsApp.

The Chat API takes `variables` per message and keeps them on the session. An
SMS or WhatsApp text to a number routed to call-init asks the server once per
conversation, as a call does, and the session runs on its answer — a stored
agent or an inline one (ADR-0017), its variables and knowledge_context — until
it expires.
"""

import json
import uuid
from typing import Any

import pytest
from aiohttp import web

from tests.harness import HARNESS_AGENT, post_signed, run_app
from tests.harness.second_instance import STUB_TEXT_REPLY
from tests.integration.limits_support import SCRIPT
from tests.integration.test_session_message_rate import _enable_whatsapp, _whatsapp
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

CUSTOMER = "+15550004444"


@pytest.fixture
def prompts(monkeypatch) -> list[str]:
    """The text LLM, stubbed: the system prompt each turn was given."""
    seen: list[str] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        seen.append(messages[0]["content"])
        return CompletionResult(text=STUB_TEXT_REPLY, total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    return seen


class CallInitServer:
    """A customer's call-init endpoint: records each request, answers
    whatever `answer` is set to."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer or {}
        self.requests: list[dict[str, Any]] = []
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> "CallInitServer":
        app = web.Application()
        app.router.add_post("/call-init", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/call-init"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append(json.loads(await request.text())["message"])
        return web.json_response(self.answer)


async def _call_init_number(
    app: Any, server_url: str, *, whatsapp: bool = False
) -> str:
    """A number routed to `server_url`, taking SMS (or WhatsApp) texts."""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
            "sms_enabled": not whatsapp,
        },
    )
    assert r.status_code == 201, r.text
    if whatsapp:
        await _enable_whatsapp(e164)
    return e164


async def _sms(app: Any, number: str, sid: str, body: str):
    return await post_signed(
        app.api,
        app.carrier,
        app.carrier.number(number).sms_url,
        {
            "AccountSid": app.carrier.account_sid,
            "MessageSid": sid,
            "From": CUSTOMER,
            "To": number,
            "Body": body,
        },
    )


async def test_chat_variables_render_and_stay_with_the_session(monkeypatch, prompts):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(
            {**HARNESS_AGENT, "system_prompt": "Ship to {{address}} for {{name}}."}
        )
        first = await app.api.post(
            "/v1/chat",
            json={
                "agent_id": agent_id,
                "message": "hi",
                "variables": {"address": "12 Herzl St", "name": "Ada"},
            },
        )
        turn = {"agent_id": agent_id, "session_id": first.json()["data"]["session_id"]}
        same = await app.api.post("/v1/chat", json={**turn, "message": "and?"})
        moved = await app.api.post(
            "/v1/chat",
            json={
                **turn,
                "message": "I moved",
                "variables": {"address": "1 Dizengoff"},
            },
        )

    assert [r.status_code for r in (first, same, moved)] == [201] * 3
    assert prompts == [
        "Ship to 12 Herzl St for Ada.",
        "Ship to 12 Herzl St for Ada.",  # a message without them keeps them
        "Ship to 1 Dizengoff for Ada.",  # merged: name kept, address replaced
    ]


async def test_chat_variables_past_the_bound_are_refused(monkeypatch, prompts):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        fifty = {f"v{n}": "x" for n in range(50)}
        too_many = await app.api.post(
            "/v1/chat",
            json={
                "agent_id": agent_id,
                "message": "hi",
                "variables": {**fifty, "a": 1},
            },
        )
        full = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "hi", "variables": fifty}
        )
        one_more = await app.api.post(
            "/v1/chat",
            json={
                "agent_id": agent_id,
                "session_id": full.json()["data"]["session_id"],
                "message": "again",
                "variables": {"a": "1"},
            },
        )

    assert too_many.status_code == 422
    assert full.status_code == 201
    # Each request is within the bound; the session they add up to is not.
    assert one_more.status_code == 400, one_more.text
    assert len(prompts) == 1


async def test_sms_to_a_call_init_number_runs_its_inline_agent(monkeypatch, prompts):
    answer = {
        "agent": {**HARNESS_AGENT, "system_prompt": "Ship to {{address}}."},
        "variables": {"address": "12 Herzl St"},
        "metadata": {"crm_id": "C-123"},
        "dynamic_data": {"knowledge_context": "Open ticket #456."},
    }
    async with (
        CallInitServer(answer) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_number(app, server.url)
        first = await _sms(app, number, "SM1", "hi")
        second = await _sms(app, number, "SM2", "and?")
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]

    expected = f"<Response><Message>{STUB_TEXT_REPLY}</Message></Response>"
    assert first.text == second.text == expected
    # Asked once, when the conversation opened — never per message.
    [asked] = server.requests
    assert asked["call"]["type"] == "inboundSms"
    assert asked["call"]["id"] == session["id"]
    assert asked["phoneNumber"]["number"] == number
    assert asked["customer"]["number"] == CUSTOMER
    assert prompts == ["Open ticket #456.\n\nShip to 12 Herzl St."] * 2
    assert session["agent_id"] is None  # an inline agent has no row
    assert "dynamic_config" not in json.dumps(session)


async def test_whatsapp_to_a_call_init_number_runs_the_agent_it_names(
    monkeypatch, prompts
):
    sent: list[str] = []

    async def _send(_http, *, text: str, **_kwargs) -> dict:
        sent.append(text)
        return {}

    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    async with (
        CallInitServer() as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        agent_id = await app.create_agent(
            {**HARNESS_AGENT, "system_prompt": "Hello {{name}}."}
        )
        server.answer = {"agent_id": agent_id, "variables": {"name": "Ada"}}
        number = await _call_init_number(app, server.url, whatsapp=True)
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp(number, "hi"))
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]

    assert r.status_code == 200
    [asked] = server.requests
    assert asked["call"]["type"] == "inboundWhatsAppMessage"
    assert sent == [STUB_TEXT_REPLY]
    assert prompts == ["Hello Ada."]
    assert session["agent_id"] == agent_id


async def test_call_init_naming_no_usable_agent_leaves_the_text_unanswered(
    monkeypatch, prompts
):
    # An agent id is not a capability: one outside the number's project — here
    # one that does not exist at all — runs nothing.
    answer = {"agent_id": str(uuid.uuid4()), "variables": {"name": "Ada"}}
    async with (
        CallInitServer(answer) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_number(app, server.url)
        r = await _sms(app, number, "SM1", "hi")
        sessions = (await app.api.get("/v1/chat/sessions")).json()["data"]

    assert r.status_code == 200
    assert STUB_TEXT_REPLY not in r.text
    assert prompts == []
    assert sessions == []


async def test_a_scenario_from_an_inline_session_targets_its_config(
    monkeypatch, prompts
):
    inline = {**HARNESS_AGENT, "system_prompt": "Built for this conversation."}
    async with (
        CallInitServer({"agent": inline}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_number(app, server.url)
        await _sms(app, number, "SM1", "hi")
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]
        r = await app.api.post(
            "/v1/eval-scenarios/from-session", json={"session_id": session["id"]}
        )

    assert r.status_code == 201, r.text
    target = r.json()["data"]["default_target"]
    assert target["type"] == "inline"
    assert target["agent"]["system_prompt"] == "Built for this conversation."
