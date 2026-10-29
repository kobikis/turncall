"""Loading a skill on a voice call (#424), over the running app.

An agent lists its project's skills by name. On a call the model is first given
only the list (each skill's name and description) and `load_skill`; when it
loads one, its next inference is given the skill's instructions as their own
prompt section, rendered with the call's `{{variables}}`, and the skill's tools
— with the conversation so far still in the context. The skill's webhook tool
reaches its webhook and is recorded like any tool, and a skill tool that takes
an agent tool's name never runs. An agent with no skills is given exactly what
it was before. A call holds at most `max_loaded_skills` loaded, a skill
already loaded loads as nothing, and a handoff brings the target agent's
skills, none loaded (#427). Seam A: the real app, stub providers, a fake
carrier.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from aiohttp import web
from loguru import logger
from redis.asyncio import Redis

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

    def __init__(
        self,
        *,
        agent_id: str = "",
        variables: dict[str, str],
        agent: dict[str, Any] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent = agent
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
            # An inline agent (no row) when one is set, else the stored one.
            chosen = (
                {"agent": self.agent} if self.agent else {"agent_id": self.agent_id}
            )
            return web.json_response({**chosen, "variables": self.variables})
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
        get_invoices = _tool(
            "get_invoices",
            f"{url}/get_invoices",
            "Fetch {{customer_name}}'s last two invoices",
        )
        bill = {
            "name": "explain_bill",
            "description": BILL,
            "instructions": "Compare the last two invoices for {{customer_name}}.",
            "tools": [get_invoices],
        }
        await _publish_skill(app, bill)
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
        # Published after the agent was saved (#428 refuses the collision at
        # save): v2's second tool takes the agent's own tool's name, and the
        # agent's keeps it.
        explain_bill = await _publish_skill(
            app,
            {
                **bill,
                "tools": [
                    get_invoices,
                    _tool("lookup_account", f"{url}/skill_lookup"),
                ],
            },
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
    assert "## Skill: explain_bill (v2)\nCompare the last two invoices for Dana." in (
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
        {"skill": "explain_bill", "version": 1, "pinned": False, "loaded": True},
        {"skill": "verify_identity", "version": 1, "pinned": False, "loaded": False},
    ]
    assert read["skills"] == skills
    assert ended["payload"]["skills"] == skills
    assert "resolved_skills" not in read["metadata"]


async def _first_inference(monkeypatch, agent: dict[str, Any]) -> Given:
    """What an agent's model is given on the caller's first turn."""
    given: list[Given] = []
    script = Script(caller=(SAID,), agent=(_remembering(given, "Hi."),))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
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
    """A stored agent cannot list such a skill (#428), but an inline call-init
    agent never passes the API's checks: a miss is a skipped skill."""
    given: list[Given] = []
    script = Script(caller=(SAID,), agent=(_remembering(given, "Hi."),))
    inline = {
        **HARNESS_AGENT,
        "first_message": "Hello.",
        "skills": [{"skill": "draft_only"}, {"skill": "never_created"}],
    }
    async with (
        Backend(variables={}, agent=inline) as backend,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        created = await app.api.post(
            "/v1/skills",
            json={
                "name": "draft_only",
                "description": "Never published.",
                "instructions": "Nothing.",
            },
        )
        assert created.status_code == 201, created.text
        number = await _bind_to_call_init(app, f"{backend.url}/call-init")
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

    [first] = given
    assert _prompt(first) == PROMPT
    assert first.tools == []
    assert ended["agent_id"] is None  # it ran the inline agent
    assert ended["payload"]["skills"] == []


async def test_an_inline_call_init_agent_loads_a_project_skill(monkeypatch):
    """A call-init inline agent has no row and never passes the API's checks
    (#431): its skills resolve against the call's project, a name the
    project does not have is a logged warning and a skipped skill, and the
    one it does have loads on the call."""
    before: list[Given] = []
    after: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            _remembering(before, ToolCall("load_skill", {"name": "explain_bill"})),
            _remembering(after, "It went up by twelve."),
        ),
    )
    inline = {
        **HARNESS_AGENT,
        "first_message": "Hello.",
        "skills": [{"skill": "explain_bill"}, {"skill": "vip_retention"}],
    }
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        async with (
            Backend(variables={"customer_name": "Dana"}, agent=inline) as backend,
            run_app(
                script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
            ) as app,
        ):
            skill = await _publish_skill(
                app,
                {
                    "name": "explain_bill",
                    "description": BILL,
                    "instructions": "Compare the invoices for {{customer_name}}.",
                    "tools": [_tool("get_invoices", f"{backend.url}/get_invoices")],
                },
            )
            number = await _bind_to_call_init(app, f"{backend.url}/call-init")
            call = await place_inbound_call(
                app.carrier, app.api, from_number=CALLER, to_number=number
            )
            await call.wait_for_agent_speech(1)  # the greeting
            await call.say(400)
            await call.wait_for_agent_speech(2)  # after the load
            await call.hang_up()
            ended = await app.sink.wait_for("call.ended")
    finally:
        logger.remove(sink)

    assert any(
        "skill_skipped" in line and "vip_retention" in line for line in warnings
    ), warnings
    [first] = before
    assert f"- explain_bill: {BILL}" in _prompt(first)
    assert "vip_retention" not in _prompt(first)
    assert first.tools == ["load_skill"]
    [second] = after
    assert "## Skill: explain_bill (v1)\nCompare the invoices for Dana." in (
        _prompt(second)
    )
    assert second.tools == ["load_skill", "get_invoices"]

    assert ended["agent_id"] is None  # the inline agent ran it
    assert ended["payload"]["skills"] == [
        {
            "skill": "explain_bill",
            "version": skill["version"],
            "pinned": False,
            "loaded": True,
        }
    ]


# --- the limit, the no-op, a handoff (#427) ----------------------------------


async def _events(app: Any, call_id: str, event_type: str, count: int) -> list:
    """The call's events of a type, once `count` are written (they are
    recorded off the switch's path)."""
    rows: list[dict[str, Any]] = []
    for _ in range(100):
        rows = (
            await app.api.get(
                f"/v1/calls/{call_id}/events", params={"event_type": event_type}
            )
        ).json()["data"]
        if len(rows) >= count:
            break
        await asyncio.sleep(0.1)
    return rows


def _skill_body(name: str, tool_url: str, instructions: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Use for {name}.",
        "instructions": instructions or f"Do {name}.",
        "tools": [_tool(f"{name}_tool", f"{tool_url}/{name}")],
    }


async def test_a_fourth_load_unloads_the_oldest_and_a_reload_changes_nothing(
    monkeypatch,
):
    held: list[Given] = []
    reloaded: list[Given] = []
    last: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall("load_skill", {"name": "first"}),
            ToolCall("load_skill", {"name": "second"}),
            ToolCall("load_skill", {"name": "third"}),
            # Holding three: the cap. Loading the third again...
            _remembering(held, ToolCall("load_skill", {"name": "third"})),
            # ...changed nothing. Now a fourth.
            _remembering(reloaded, ToolCall("load_skill", {"name": "fourth"})),
            _remembering(last, "Done."),
        ),
    )
    names = ("first", "second", "third", "fourth")
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        for name in names:
            await _publish_skill(app, _skill_body(name, "http://127.0.0.1:9"))
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "skills": [{"skill": n} for n in names],
                }
            )
        )
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)  # "Done."
        unloaded = await _events(app, call.call_id, "skill.unloaded", 1)
        loaded = await _events(app, call.call_id, "skill.loaded", 4)
        delivered = await app.sink.wait_for("skill.unloaded")
        await call.hang_up()
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    # The re-load: the same prompt and the same tools as before it.
    [before], [after] = held, reloaded
    assert _prompt(after) == _prompt(before)
    assert after.tools == before.tools
    assert before.tools == ["load_skill", "first_tool", "second_tool", "third_tool"]
    assert any(
        m["role"] == "tool" and "already loaded" in str(m.get("content")) for m in after
    ), list(after)

    # The fourth: the newest three's sections and tools, not the oldest's.
    [given] = last
    prompt = _prompt(given)
    assert "## Skill: first" not in prompt, prompt
    for name in ("second", "third", "fourth"):
        assert f"## Skill: {name} (v1)\nDo {name}." in prompt
    assert "- first: Use for first." in prompt  # still loadable
    assert given.tools == ["load_skill", "second_tool", "third_tool", "fourth_tool"]
    assert {"role": "user", "content": SAID} in given

    # Recorded: one unload, for the limit; the re-load is no load at all.
    gone = {"skill": "first", "version": 1, "reason": "limit"}
    assert [e["payload"] for e in unloaded] == [gone]
    assert delivered["payload"] == gone and delivered["_signature_valid"]
    assert [e["payload"]["skill"] for e in loaded] == list(names)
    # The read says what the call loaded at any point; the unload is an event.
    assert read["skills"] == [
        {"skill": n, "version": 1, "pinned": False, "loaded": True} for n in names
    ]


@pytest.fixture
async def redis_up():
    """The API's handoff runs over the call's control channel, on Redis."""
    client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable ({type(exc).__name__})")
    finally:
        await client.aclose()


BILLING_PROMPT = "You are the billing agent."
END_CALL = {
    "name": "end_call",
    "description": "End the call",
    "parameters_schema": {"type": "object", "properties": {}},
}


async def test_a_handoff_brings_the_targets_skills_none_loaded(monkeypatch, redis_up):
    after_handoff: list[Given] = []
    after_load: list[Given] = []
    script = Script(
        caller=(SAID, "My date of birth is March 3rd."),
        agent=(
            ToolCall("load_skill", {"name": "explain_bill"}),
            "Let me check.",
            _remembering(after_handoff, ToolCall("load_skill", {"name": "verify"})),
            _remembering(after_load, "Thanks."),
        ),
    )
    async with (
        Backend(variables={"customer_name": "Dana"}) as backend,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        url = backend.url
        await _publish_skill(app, _skill_body("explain_bill", url))
        await _publish_skill(
            app,
            _skill_body("verify", url, "Ask {{customer_name}} for the date of birth."),
        )
        backend.agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "skills": [{"skill": "explain_bill"}],
            }
        )
        # The target lists the skill the call already loaded, too: it starts
        # with none loaded all the same.
        billing = await app.create_agent(
            {
                **HARNESS_AGENT,
                "system_prompt": BILLING_PROMPT,
                "tools": [END_CALL],
                "skills": [{"skill": "verify"}, {"skill": "explain_bill"}],
            },
            name="billing",
        )
        number = await _bind_to_call_init(app, f"{url}/call-init")
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)  # "Let me check.", explain_bill loaded
        handed = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff", json={"target_agent_id": billing}
        )
        assert handed.status_code == 200, handed.text
        await call.say(400)
        await call.wait_for_agent_speech(3)  # "Thanks."
        await _events(app, call.call_id, "skill.loaded", 2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    assert handed.json()["data"]["details"]["tools"] == ["load_skill", "end_call"]

    # The target's list, none loaded — not even the skill the call had loaded.
    [given] = after_handoff
    prompt = _prompt(given)
    assert prompt.startswith(BILLING_PROMPT), prompt
    assert PROMPT not in prompt
    assert "- verify: Use for verify." in prompt
    assert "- explain_bill: Use for explain_bill." in prompt
    assert "## Skill:" not in prompt
    assert given.tools == ["load_skill", "end_call"]

    # And a target skill loads, rendered with the call's variables.
    [given] = after_load
    prompt = _prompt(given)
    assert "## Skill: verify (v1)\nAsk Dana for the date of birth." in prompt
    assert "## Skill: explain_bill" not in prompt
    assert given.tools == ["load_skill", "end_call", "verify_tool"]

    # Both agents' resolutions on the record, the shared version once.
    skills = [
        {"skill": "explain_bill", "version": 1, "pinned": False, "loaded": True},
        {"skill": "verify", "version": 1, "pinned": False, "loaded": True},
    ]
    assert read["skills"] == skills
    assert ended["payload"]["skills"] == skills


# --- pins and the live version (#426) ----------------------------------------


async def _ended(app: Any, call_id: str) -> dict[str, Any]:
    """The `call.ended` a subscriber got for this call."""
    for _ in range(200):
        for envelope in app.sink.of("call.ended"):
            if envelope["call_id"] == call_id:
                return dict(envelope)
        await asyncio.sleep(0.1)
    raise AssertionError(f"no call.ended for {call_id}")


async def _call_and_load(app: Any, number: str) -> tuple[str, dict[str, Any]]:
    """One call on which the model loads `verify_identity`: its id and its
    `call.ended`."""
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)
    await call.say(400)
    await call.wait_for_agent_speech(2)
    await call.hang_up()
    return call.call_id, await _ended(app, call.call_id)


async def test_a_pin_keeps_its_version_and_followers_get_each_publish_and_rollback(
    monkeypatch,
):
    """Two agents on one skill: `support` follows the live version, and
    `collections` pins v1. Neither agent is touched after it is saved. Each
    publish and the rollback reach `support`'s next call; `collections` runs
    v1 throughout, archived from the first publish on. The call read,
    `call.ended` and the skill's agents list say which entries were pinned."""
    loaded: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall("load_skill", {"name": "verify_identity"}),
            _remembering(loaded, "Verified."),
        ),
    )

    def verify(version: int) -> dict[str, Any]:
        return {
            "name": "verify_identity",
            "description": VERIFY,
            "instructions": f"Verify, as version {version} says.",
        }

    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        v1 = await _publish_skill(app, verify(1))
        agent = {**HARNESS_AGENT, "first_message": "Hello."}
        support = await app.bind_number(
            await app.create_agent(
                {**agent, "skills": [{"skill": "verify_identity"}]}, "support"
            )
        )
        collections = await app.bind_number(
            await app.create_agent(
                {
                    **agent,
                    "skills": [{"skill": "verify_identity", "version_id": v1["id"]}],
                },
                "collections",
            )
        )

        await _publish_skill(app, verify(2))  # v1 archived
        v3 = await _publish_skill(app, verify(3))  # v2 archived
        listing = (await app.api.get("/v1/skills/verify_identity/agents")).json()
        ran = [await _call_and_load(app, support)]
        pinned_call, pinned_ended = await _call_and_load(app, collections)
        pinned_read = (await app.api.get(f"/v1/calls/{pinned_call}")).json()["data"]

        v2_id = (
            await app.api.get(
                "/v1/skills", params={"name": "verify_identity", "state": "archived"}
            )
        ).json()["data"]
        [v2] = [s for s in v2_id if s["version"] == 2]
        rolled = await app.api.post(f"/v1/skills/{v2['id']}/rollback")
        assert rolled.status_code == 200, rolled.text
        ran.append(await _call_and_load(app, support))
        ran.append(await _call_and_load(app, collections))
        follower_read = (await app.api.get(f"/v1/calls/{ran[0][0]}")).json()["data"]

    sections = [
        next(line for line in _prompt(g).splitlines() if line.startswith("## Skill:"))
        for g in loaded
    ]
    assert sections == [
        "## Skill: verify_identity (v3)",  # support, after two publishes
        "## Skill: verify_identity (v1)",  # collections, its pin archived
        "## Skill: verify_identity (v2)",  # support, after the rollback
        "## Skill: verify_identity (v1)",  # collections, still
    ]
    assert "Verify, as version 1 says." in _prompt(loaded[1])

    following = [{"skill": "verify_identity", "version": 3, "pinned": False}]
    pinned = [{"skill": "verify_identity", "version": 1, "pinned": True}]
    assert [
        {k: s[k] for k in ("skill", "version", "pinned")}
        for s in ran[0][1]["payload"]["skills"]
    ] == following
    assert follower_read["skills"] == [{**following[0], "loaded": True}]
    assert pinned_ended["payload"]["skills"] == [{**pinned[0], "loaded": True}]
    assert pinned_read["skills"] == pinned_ended["payload"]["skills"]
    assert [s["version"] for s in ran[1][1]["payload"]["skills"]] == [2]
    assert ran[2][1]["payload"]["skills"] == [{**pinned[0], "loaded": True}]

    rows = {r["agent_name"]: r for r in listing["data"]}
    assert (rows["support"]["resolved_skill_id"], rows["support"]["pinned"]) == (
        v3["id"],
        False,
    )
    assert (
        rows["collections"]["resolved_skill_id"],
        rows["collections"]["resolved_version"],
        rows["collections"]["pinned"],
    ) == (v1["id"], 1, True)
