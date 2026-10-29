"""Loading a skill on a voice call (#424), over the running app.

An agent lists its project's skills by name. On a call the model is first given
only the list (each skill's name and description) and `load_skill`; when it
loads one, its next inference is given the skill's instructions as their own
prompt section, rendered with the call's `{{variables}}`, and the skill's tools
— with the conversation so far still in the context. The skill's webhook tool
reaches its webhook and is recorded like any tool, and a skill tool that takes
an agent tool's name never runs. An agent with no skills is given exactly what
it was before. Seam A: the real app, stub providers, a fake carrier.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from aiohttp import web

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.twilio import place_inbound_call

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
SAID = "My bill went up this month."
PROMPT = HARNESS_AGENT["system_prompt"]
BILL = "Use when the caller asks why a bill changed or what a charge is."
VERIFY = "Use before discussing anything account-specific."


class Backend:
    """The customer's server: call-init on `/call-init`, every tool's webhook
    on its own path. Records each request's path and JSON body."""

    def __init__(self, *, agent_id: str = "", variables: dict[str, str]) -> None:
        self.agent_id = agent_id
        self.variables = variables
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> Backend:
        app = web.Application()
        app.router.add_post("/{path:.*}", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, await request.json()))
        if request.path == "/call-init":
            return web.json_response(
                {"agent_id": self.agent_id, "variables": self.variables}
            )
        return web.json_response({"answered_by": request.path})

    def tool_paths(self) -> list[str]:
        return [path for path, _ in self.requests if path != "/call-init"]


def _tool(name: str, url: str, description: str = "A tool") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters_schema": {"type": "object", "properties": {}},
        "webhook_url": url,
    }


def _remembering(given: list[Given], reply: Any):
    def answer(context: Given) -> Any:
        given.append(context)
        return reply

    return answer


def _prompt(given: Given) -> str:
    return next((m["content"] for m in given if m["role"] == "system"), "")


async def _publish_skill(app: Any, body: dict[str, Any]) -> dict[str, Any]:
    created = await app.api.post("/v1/skills", json=body)
    assert created.status_code == 201, created.text
    skill = created.json()["data"]
    published = await app.api.post(f"/v1/skills/{skill['id']}/publish")
    assert published.status_code == 200, published.text
    return dict(published.json()["data"])


async def _bind_to_call_init(app: Any, server_url: str) -> str:
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    bound = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
        },
    )
    assert bound.status_code == 201, bound.text
    return e164


async def _invocations(app: Any, call_id: str, count: int) -> list[dict[str, Any]]:
    """The call's tool invocation rows, once `count` of them are written (they
    are recorded off the tool's critical path)."""
    rows: list[dict[str, Any]] = []
    for _ in range(100):
        rows = (await app.api.get(f"/v1/tools/invocations/{call_id}")).json()["data"]
        if len(rows) >= count:
            break
        await asyncio.sleep(0.1)
    return rows


async def test_a_loaded_skill_brings_its_instructions_and_tools(monkeypatch):
    before: list[Given] = []
    after: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            _remembering(before, ToolCall("load_skill", {"name": "explain_bill"})),
            _remembering(
                after,
                (ToolCall("get_invoices", {}), ToolCall("lookup_account", {})),
            ),
            "It went up by twelve.",
        ),
    )
    async with (
        Backend(variables={"customer_name": "Dana"}) as backend,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        url = backend.url
        explain_bill = await _publish_skill(
            app,
            {
                "name": "explain_bill",
                "description": BILL,
                "instructions": "Compare the last two invoices for {{customer_name}}.",
                "tools": [
                    _tool(
                        "get_invoices",
                        f"{url}/get_invoices",
                        "Fetch {{customer_name}}'s last two invoices",
                    ),
                    # Takes the agent's own tool's name: the agent's keeps it.
                    _tool("lookup_account", f"{url}/skill_lookup"),
                ],
            },
        )
        await _publish_skill(
            app,
            {
                "name": "verify_identity",
                "description": VERIFY,
                "instructions": "Ask for the date of birth.",
                "tools": [_tool("verify_customer", f"{url}/verify_customer")],
            },
        )
        backend.agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "tools": [_tool("lookup_account", f"{url}/agent_lookup")],
                "skills": [{"skill": "explain_bill"}, {"skill": "verify_identity"}],
            }
        )
        number = await _bind_to_call_init(app, f"{url}/call-init")

        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        await call.say(400)
        await call.wait_for_agent_speech(2)  # after the skill's tools answered
        rows = await _invocations(app, call.call_id, 3)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

    # Before the load: the list and load_skill, none of the skills' tools.
    [first] = before
    prompt = _prompt(first)
    assert prompt.startswith(PROMPT), prompt
    assert "## Skills you can load" in prompt
    assert f"- explain_bill: {BILL}" in prompt
    assert f"- verify_identity: {VERIFY}" in prompt
    assert "## Skill:" not in prompt
    assert first.tools == ["load_skill", "lookup_account"]

    # After it: the skill's section, rendered, and its tools — in precedence
    # order — over the conversation as it was.
    [second] = after
    prompt = _prompt(second)
    assert "## Skill: explain_bill (v1)\nCompare the last two invoices for Dana." in (
        prompt
    )
    assert "## Skill: verify_identity" not in prompt
    assert "## Skills you can load" in prompt
    assert second.tools == ["load_skill", "lookup_account", "get_invoices"]
    assert {"role": "user", "content": SAID} in second, list(second)
    assert any(
        m["role"] == "tool" and "is loaded" in str(m.get("content")) for m in second
    ), list(second)

    # The skill's tool reached its webhook; the agent's kept its name.
    assert sorted(backend.tool_paths()) == ["/agent_lookup", "/get_invoices"]
    [(_, posted)] = [r for r in backend.requests if r[0] == "/get_invoices"]
    assert posted["tool_name"] == "get_invoices"
    assert posted["call_id"] == call.call_id

    # Recorded like any tool, the load included.
    by_name = {r["tool_name"]: r for r in rows}
    assert set(by_name) == {"load_skill", "get_invoices", "lookup_account"}, rows
    assert by_name["get_invoices"]["status"] == "succeeded"
    assert by_name["load_skill"]["output_json"]["details"] == {
        "skill": "explain_bill",
        "version": explain_bill["version"],
    }

    said = [(t["role"], t["text"]) for t in ended["payload"]["transcript"]]
    assert ("agent", "It went up by twelve.") in said, said


async def test_a_call_records_the_skill_versions_it_ran(monkeypatch):
    """#425. The call resolves its skills once, at its start: a version
    published while it is live is not the one it loads. The load is a
    `skill.loaded` event on the call's log and at a subscriber, and the call
    read and `call.ended` list the same skills, the one never loaded too."""
    after: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall("load_skill", {"name": "explain_bill"}),
            _remembering(after, "It went up by twelve."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _publish_skill(
            app,
            {"name": "explain_bill", "description": BILL, "instructions": "Old."},
        )
        await _publish_skill(
            app,
            {"name": "verify_identity", "description": VERIFY, "instructions": "Ask."},
        )
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "skills": [{"skill": "explain_bill"}, {"skill": "verify_identity"}],
                }
            )
        )

        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting: the call is built
        newer = await _publish_skill(
            app,
            {"name": "explain_bill", "description": BILL, "instructions": "New."},
        )
        await call.say(400)
        await call.wait_for_agent_speech(2)
        delivered = await app.sink.wait_for("skill.loaded")
        events = (
            await app.api.get(
                f"/v1/calls/{call.call_id}/events",
                params={"event_type": "skill.loaded"},
            )
        ).json()["data"]
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    # Published mid-call, and not what the call loaded.
    assert newer["version"] == 2
    [given] = after
    prompt = _prompt(given)
    assert "## Skill: explain_bill (v1)\nOld." in prompt, prompt
    assert "New." not in prompt

    loaded = {"skill": "explain_bill", "version": 1, "reason": "load_skill"}
    assert [e["payload"] for e in events] == [loaded]
    assert delivered["payload"] == loaded
    assert delivered["call_id"] == call.call_id and delivered["_signature_valid"]

    skills = [
        {"skill": "explain_bill", "version": 1, "loaded": True},
        {"skill": "verify_identity", "version": 1, "loaded": False},
    ]
    assert read["skills"] == skills
    assert ended["payload"]["skills"] == skills
    assert "resolved_skills" not in read["metadata"]


async def _first_inference(monkeypatch, agent: dict[str, Any], skills=()) -> Given:
    """What an agent's model is given on the caller's first turn."""
    given: list[Given] = []
    script = Script(caller=(SAID,), agent=(_remembering(given, "Hi."),))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        for body, publish in skills:
            created = await app.api.post("/v1/skills", json=body)
            assert created.status_code == 201, created.text
            if publish:
                skill_id = created.json()["data"]["id"]
                await app.api.post(f"/v1/skills/{skill_id}/publish")
        number = await app.bind_number(
            await app.create_agent(
                {**HARNESS_AGENT, "first_message": "Hello.", **agent}
            )
        )
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
    [first] = given
    return first


async def test_an_agent_without_skills_is_given_what_it_always_was(monkeypatch):
    given = await _first_inference(monkeypatch, {})

    assert _prompt(given) == PROMPT
    assert given.tools == []


async def test_a_skill_that_does_not_resolve_is_skipped_and_the_call_goes_on(
    monkeypatch,
):
    draft = {
        "name": "draft_only",
        "description": "Never published.",
        "instructions": "Nothing.",
    }
    given = await _first_inference(
        monkeypatch,
        {"skills": [{"skill": "draft_only"}, {"skill": "never_created"}]},
        skills=[(draft, False)],
    )

    assert _prompt(given) == PROMPT
    assert given.tools == []
