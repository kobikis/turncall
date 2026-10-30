"""Skills in a text conversation (#430): the Chat API, SMS and WhatsApp text.

An agent's skills reach a text turn as on a call: the prompt lists them and
the tool loop offers `load_skill`. A load gives the turn's next round the
skill's section and tools, and the session keeps it: the next message of the
session holds it without loading again, a new session starts with none, and
`max_loaded_skills` unloads the oldest. SMS and WhatsApp texts take the same
path as the Chat API.

The text LLM is scripted (`TextLLM`): each round's reply is the next step of
its script, run through the turn's real tool executor and `refresh`, so what
it records is exactly what each round of the real loop would send.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, post_signed, run_app
from tests.integration.limits_support import SCRIPT
from tests.integration.test_harness_call_skills import (
    Backend,
    _publish_skill,
    _skill_body,
    _tool,
)
from tests.integration.test_session_message_rate import _sms_number, _whatsapp
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

BILL = "Use when the customer asks why a bill changed."
REPLY = "Your bill went up by a tariff adjustment."


@dataclass(frozen=True)
class Round:
    """What one round of a text turn was given."""

    prompt: str
    tools: list[str]
    said: list[str]  # the customer's messages, oldest first


class TextLLM:
    """The text LLM, scripted: a step is a reply (str) or the tool calls to
    make (`[(name, args)]`), consumed one per round across every turn."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        # Each turn, while any are left, runs the next of these instead.
        self.turns: list[list[Any]] = []
        self.rounds: list[Round] = []
        self.results: list[dict[str, Any]] = []

    async def __call__(self, _config, messages, **kwargs) -> CompletionResult:
        prompt = messages[0]["content"]
        tools = [t["name"] for t in kwargs.get("tools") or []]
        said = [m["content"] for m in messages if m["role"] == "user"]
        refresh = kwargs.get("refresh")
        script = self.turns.pop(0) if self.turns else self.script
        while True:
            self.rounds.append(Round(prompt, tools, said))
            step = script.pop(0)
            if callable(step):
                await step()
                step = script.pop(0)
            if isinstance(step, str):
                return CompletionResult(text=step, total_tokens=1)
            for name, args in step:
                result = await kwargs["execute_tool"](name, args)
                self.results.append(json.loads(result))
            if refresh is not None:
                update = refresh()
                prompt = update.system_prompt
                tools = [t["name"] for t in update.tools]


@pytest.fixture
def llm(monkeypatch) -> TextLLM:
    fake = TextLLM()
    monkeypatch.setattr("turncall.services.sms_chat.complete_text", fake)
    return fake


async def _skilled_agent(app: Any, backend: Backend, **config: Any) -> str:
    await _publish_skill(
        app,
        {
            **_skill_body("explain_bill", backend.url),
            "description": BILL,
            "instructions": "Compare the last two invoices.",
            "tools": [_tool("get_invoices", f"{backend.url}/invoices")],
        },
    )
    return await app.create_agent(
        {**HARNESS_AGENT, "skills": [{"skill": "explain_bill"}], **config}
    )


async def _invocations(app: Any, session_id: str, count: int) -> list[str]:
    """The session's tool invocations by name, once `count` are written."""
    names: list[str] = []
    for _ in range(100):
        r = await app.api.get(f"/v1/chat/sessions/{session_id}/tool-invocations")
        names = sorted(row["tool_name"] for row in r.json()["data"])
        if len(names) >= count:
            break
        await asyncio.sleep(0.1)
    return names


async def test_a_loaded_skill_answers_and_stays_with_the_session(monkeypatch, llm):
    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        agent_id = await _skilled_agent(app, backend)
        llm.script = [
            [("load_skill", {"name": "explain_bill"})],
            [("get_invoices", {})],
            REPLY,
        ]
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Why is it higher?"}
        )
        session_id = first.json()["data"]["session_id"]
        llm.script = ["And it is due on the 15th."]
        second = await app.api.post(
            "/v1/chat",
            json={"agent_id": agent_id, "session_id": session_id, "message": "Due?"},
        )
        llm.script = ["Hello again."]
        fresh = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Hi"}
        )
        recorded = await _invocations(app, session_id, 2)

    assert [r.status_code for r in (first, second, fresh)] == [201] * 3
    assert first.json()["data"]["reply"] == REPLY
    before, loaded, answered, later, new_session = llm.rounds
    # Before the load: the list and `load_skill`, none of the skill's own.
    assert "## Skills you can load" in before.prompt
    assert f"- explain_bill: {BILL}" in before.prompt
    assert "## Skill: explain_bill" not in before.prompt
    assert before.tools == ["load_skill"]
    # The next round of the same turn has the skill's section and tool.
    assert "## Skill: explain_bill (v1)\nCompare the last two invoices." in (
        loaded.prompt
    )
    assert loaded.tools == ["load_skill", "get_invoices"]
    assert answered.tools == loaded.tools
    # The skill's tool reached its webhook, from the session.
    [(path, body)] = backend.requests
    assert path == "/invoices"
    assert body["session_id"] == session_id and body["call_id"] is None
    assert llm.results[0]["details"] == {"skill": "explain_bill", "version": 1}
    # The next message holds it without loading it again.
    assert "## Skill: explain_bill (v1)" in later.prompt
    assert later.tools == ["load_skill", "get_invoices"]
    assert later.said == ["Why is it higher?", "Due?"]
    # A new session starts with nothing loaded.
    assert new_session.tools == ["load_skill"]
    assert "## Skill: explain_bill" not in new_session.prompt
    assert fresh.json()["data"]["session_id"] != session_id
    # The load is recorded like any text tool.
    assert recorded == ["get_invoices", "load_skill"]


async def test_the_limit_unloads_the_oldest_on_text(monkeypatch, llm):
    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        for name in ("verify_identity", "explain_bill"):
            await _publish_skill(app, _skill_body(name, backend.url))
        agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "skills": [{"skill": "verify_identity"}, {"skill": "explain_bill"}],
                "max_loaded_skills": 1,
            }
        )
        llm.script = [[("load_skill", {"name": "verify_identity"})], "Verified."]
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Hi"}
        )
        turn = {"agent_id": agent_id, "session_id": first.json()["data"]["session_id"]}
        llm.script = [
            [("load_skill", {"name": "explain_bill"})],
            [("load_skill", {"name": "explain_bill"})],
            "Here is your bill.",
        ]
        await app.api.post("/v1/chat", json={**turn, "message": "My bill?"})
        llm.script = ["Anything else?"]
        await app.api.post("/v1/chat", json={**turn, "message": "Thanks"})

    unloaded, again = llm.results[1], llm.results[2]
    assert unloaded["details"] == {
        "skill": "explain_bill",
        "version": 1,
        "unloaded": ["verify_identity"],
    }
    assert "'verify_identity' was unloaded" in unloaded["message"]
    assert again["details"]["already_loaded"] is True
    last = llm.rounds[-1]
    assert "## Skill: explain_bill (v1)" in last.prompt
    assert "## Skill: verify_identity" not in last.prompt
    assert last.tools == ["load_skill", "explain_bill_tool"]


async def test_sms_and_whatsapp_texts_hold_skills_the_same_way(monkeypatch, llm):
    sent: list[str] = []

    async def _send(_http, *, text: str, **_kwargs) -> dict:
        sent.append(text)
        return {}

    monkeypatch.setattr("turncall.services.whatsapp_chat.send_whatsapp_text", _send)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        agent_id = await _skilled_agent(app, backend)
        sms_number = await _sms_number(app, agent_id)
        url = app.carrier.number(sms_number).sms_url

        def sms(sid: str, body: str) -> dict[str, str]:
            return {
                "AccountSid": app.carrier.account_sid,
                "MessageSid": sid,
                "From": "+15550004444",
                "To": sms_number,
                "Body": body,
            }

        llm.script = [[("load_skill", {"name": "explain_bill"})], REPLY]
        loaded = await post_signed(app.api, app.carrier, url, sms("SM1", "Why?"))
        llm.script = ["Due on the 15th."]
        held = await post_signed(app.api, app.carrier, url, sms("SM2", "When?"))

        wa_number = await _sms_number(app, agent_id, whatsapp=True)
        llm.script = [
            [("load_skill", {"name": "explain_bill"})],
            [("get_invoices", {})],
            REPLY,
        ]
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp(wa_number, "Why?"))

    assert loaded.text == f"<Response><Message>{REPLY}</Message></Response>"
    assert held.status_code == 200
    sms_held = llm.rounds[2]
    assert "## Skill: explain_bill (v1)" in sms_held.prompt
    assert sms_held.tools == ["load_skill", "get_invoices"]
    assert r.status_code == 200
    assert sent == [REPLY]
    assert [path for path, _ in backend.requests] == ["/invoices"]


async def test_a_session_keeps_the_versions_it_resolved(monkeypatch, llm):
    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        agent_id = await _skilled_agent(app, backend)
        llm.script = ["Hi."]
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Hi"}
        )
        turn = {"agent_id": agent_id, "session_id": first.json()["data"]["session_id"]}
        await _publish_skill(
            app, {**_skill_body("explain_bill", backend.url), "description": "v2"}
        )
        llm.script = [[("load_skill", {"name": "explain_bill"})], "Loaded."]
        await app.api.post("/v1/chat", json={**turn, "message": "My bill?"})
        llm.script = ["A new conversation."]
        await app.api.post("/v1/chat", json={"agent_id": agent_id, "message": "Hi"})

    # Published mid-session: the session runs the version it resolved first.
    assert llm.results[0]["details"] == {"skill": "explain_bill", "version": 1}
    assert f"- explain_bill: {BILL}" in llm.rounds[1].prompt
    # A new session resolves the live one.
    assert "- explain_bill: v2" in llm.rounds[-1].prompt


async def test_two_messages_at_once_lose_neither_load(monkeypatch, llm):
    both_in_the_llm = asyncio.Barrier(2)

    async def meet() -> None:
        await asyncio.wait_for(both_in_the_llm.wait(), 10)

    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        for name in ("verify_identity", "explain_bill"):
            await _publish_skill(app, _skill_body(name, backend.url))
        agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "skills": [{"skill": "verify_identity"}, {"skill": "explain_bill"}],
            }
        )
        llm.script = ["Hi."]
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Hi"}
        )
        turn = {"agent_id": agent_id, "session_id": first.json()["data"]["session_id"]}
        # Both turns are inside the LLM before either loads: each began with
        # nothing loaded, and each stores its load after the other began.
        llm.turns = [
            [meet, [("load_skill", {"name": "verify_identity"})], "ok"],
            [meet, [("load_skill", {"name": "explain_bill"})], "ok"],
        ]
        await asyncio.gather(
            app.api.post("/v1/chat", json={**turn, "message": "verify me"}),
            app.api.post("/v1/chat", json={**turn, "message": "my bill"}),
        )
        llm.script = ["Both are loaded."]
        await app.api.post("/v1/chat", json={**turn, "message": "and now?"})

    last = llm.rounds[-1]
    assert "## Skill: verify_identity (v1)" in last.prompt
    assert "## Skill: explain_bill (v1)" in last.prompt
    assert set(last.tools) == {
        "load_skill",
        "verify_identity_tool",
        "explain_bill_tool",
    }


async def test_a_linked_project_loads_its_librarys_skill_on_text(monkeypatch, llm):
    """#433: a text session in a project linked to a skill library resolves
    the agent's skills there, loads one, and keeps it on the next message."""

    class _Library:
        def __init__(self, api: Any) -> None:
            self.api = api

    async with (
        Backend(variables={}) as backend,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        app.another_project("library") as (library, lib),
    ):
        skill = await _publish_skill(
            _Library(lib),
            {
                **_skill_body("explain_bill", backend.url),
                "description": BILL,
                "instructions": "Compare the last two invoices.",
                "tools": [_tool("get_invoices", f"{backend.url}/invoices")],
            },
        )
        assert (await app.link_skill_library(library)).status_code == 200
        agent_id = await app.create_agent(
            {**HARNESS_AGENT, "skills": [{"skill": "explain_bill"}]}
        )
        llm.script = [
            [("load_skill", {"name": "explain_bill"})],
            [("get_invoices", {})],
            REPLY,
        ]
        first = await app.api.post(
            "/v1/chat", json={"agent_id": agent_id, "message": "Why is it higher?"}
        )
        session_id = first.json()["data"]["session_id"]
        llm.script = ["And it is due on the 15th."]
        second = await app.api.post(
            "/v1/chat",
            json={"agent_id": agent_id, "session_id": session_id, "message": "Due?"},
        )

    assert [r.status_code for r in (first, second)] == [201] * 2
    before, loaded, _answered, later = llm.rounds
    assert f"- explain_bill: {BILL}" in before.prompt
    assert before.tools == ["load_skill"]
    assert "## Skill: explain_bill (v1)\nCompare the last two invoices." in (
        loaded.prompt
    )
    assert loaded.tools == ["load_skill", "get_invoices"]
    assert llm.results[0]["details"] == {"skill": "explain_bill", "version": 1}
    assert [path for path, _ in backend.requests] == ["/invoices"]
    # The next message holds the library's version without loading it again.
    assert "## Skill: explain_bill (v1)" in later.prompt
    assert later.tools == ["load_skill", "get_invoices"]
    assert skill["version"] == 1
