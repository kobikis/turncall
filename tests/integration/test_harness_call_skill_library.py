"""Skills from a skill library on a voice call (#432, ADR-0031), over the
running app.

A project linked to a library resolves every skill name there and only there:
a call-init inline agent's skill only the library holds is listed and loads,
and the call's record names the library's version; a skill only the project
itself holds, or a pin on one of its own versions, is a warning and a skipped
skill. Unlinked, the same call skips the library's skill and runs. A
handoff's target resolves through the library too. Seam A: the real app,
stub providers, a fake carrier.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from loguru import logger
from redis.asyncio import Redis

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.twilio import place_inbound_call
from tests.integration.test_harness_call_skills import (
    BILL,
    CALLER,
    SAID,
    Backend,
    _bind_to_call_init,
    _events,
    _prompt,
    _publish_skill,
    _remembering,
    _tool,
)

pytestmark = pytest.mark.integration


def _bill(url: str) -> dict[str, Any]:
    return {
        "name": "explain_bill",
        "description": BILL,
        "instructions": "Compare the invoices for {{customer_name}}.",
        "tools": [_tool("get_invoices", f"{url}/get_invoices")],
    }


class _Warnings:
    """The WARNING lines logged while it is open."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._sink = 0

    def __enter__(self) -> _Warnings:
        self._sink = logger.add(lambda m: self.lines.append(str(m)), level="WARNING")
        return self

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink)

    def any(self, *words: str) -> bool:
        return any(all(w in line for w in words) for line in self.lines)


async def _one_call(app: Any, number: str) -> dict[str, Any]:
    """A call on which the caller speaks once and the agent answers twice
    after the greeting; its `call.ended`."""
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    await call.say(400)
    await call.wait_for_agent_speech(2)
    await call.hang_up()
    return await app.sink.wait_for("call.ended")


async def test_an_inline_agent_in_a_linked_project_loads_a_library_skill(
    monkeypatch,
):
    before: list[Given] = []
    after: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            _remembering(before, ToolCall("load_skill", {"name": "explain_bill"})),
            _remembering(after, "It went up by twelve."),
        ),
    )
    with _Warnings() as warnings:
        async with (
            Backend(variables={"customer_name": "Dana"}) as backend,
            run_app(
                script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
            ) as app,
            app.another_project("library") as (library, lib),
        ):
            await _publish_skill(_Api(lib), _bill(backend.url))  # v1
            skill = await _publish_skill(_Api(lib), _bill(backend.url))  # v2, live
            # The project's own: neither is where a linked project looks.
            await _publish_skill(app, {**_bill(backend.url), "name": "local_only"})
            own = await _publish_skill(app, {**_bill(backend.url), "name": "verify"})
            await _publish_skill(_Api(lib), {**_bill(backend.url), "name": "verify"})
            backend.agent = {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "skills": [
                    {"skill": "explain_bill"},
                    {"skill": "local_only"},
                    {"skill": "verify", "version_id": own["id"]},
                ],
            }
            linked = await app.link_skill_library(library)
            assert linked.status_code == 200, linked.text
            number = await _bind_to_call_init(app, f"{backend.url}/call-init")
            ended = await _one_call(app, number)

    [first] = before
    assert f"- explain_bill: {BILL}" in _prompt(first)
    assert "local_only" not in _prompt(first)
    assert "- verify:" not in _prompt(first)
    assert first.tools == ["load_skill"]
    [second] = after
    assert "## Skill: explain_bill (v2)\nCompare the invoices for Dana." in (
        _prompt(second)
    )
    assert second.tools == ["load_skill", "get_invoices"]
    assert warnings.any("skill_skipped", "local_only", "skill library")
    assert warnings.any("skill_skipped", "verify", own["id"])

    assert ended["agent_id"] is None  # the inline agent ran it
    assert ended["payload"]["skills"] == [
        {"skill": "explain_bill", "version": 2, "pinned": False, "loaded": True}
    ]
    assert skill["version"] == 2


async def test_the_same_call_unlinked_skips_the_library_skill_and_runs(monkeypatch):
    given: list[Given] = []
    script = Script(caller=(SAID,), agent=(_remembering(given, "Hello again."),))
    with _Warnings() as warnings:
        async with (
            Backend(variables={"customer_name": "Dana"}) as backend,
            run_app(
                script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
            ) as app,
            app.another_project("library") as (_, lib),
        ):
            await _publish_skill(_Api(lib), _bill(backend.url))
            backend.agent = {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "skills": [{"skill": "explain_bill"}],
            }
            number = await _bind_to_call_init(app, f"{backend.url}/call-init")
            call = await place_inbound_call(
                app.carrier, app.api, from_number=CALLER, to_number=number
            )
            await call.wait_for_agent_speech(1)
            await call.say(400)
            await call.wait_for_agent_speech(2)
            await call.hang_up()
            ended = await app.sink.wait_for("call.ended")

    assert warnings.any("skill_skipped", "explain_bill", "this project")
    [first] = given
    assert "explain_bill" not in _prompt(first)
    assert "load_skill" not in first.tools
    assert ended["payload"]["status"] == "completed"
    assert ended["payload"]["skills"] == []


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


async def test_a_handoff_target_resolves_through_the_library(monkeypatch, redis_up):
    after_handoff: list[Given] = []
    after_load: list[Given] = []
    script = Script(
        caller=(SAID, "My date of birth is March 3rd."),
        agent=(
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
        app.another_project("library") as (library, lib),
    ):
        verify = {
            "name": "verify",
            "description": "Use for verify.",
            "instructions": "Ask {{customer_name}} for the date of birth.",
            "tools": [_tool("verify_tool", f"{backend.url}/verify")],
        }
        skill = await _publish_skill(_Api(lib), verify)
        linked = await app.link_skill_library(library)
        assert linked.status_code == 200, linked.text
        backend.agent_id = await app.create_agent(
            {**HARNESS_AGENT, "first_message": "Hello."}
        )
        billing = await app.create_agent(
            {**HARNESS_AGENT, "skills": [{"skill": "verify"}]}, name="billing"
        )
        number = await _bind_to_call_init(app, f"{backend.url}/call-init")
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)  # "Let me check."
        handed = await app.api.post(
            f"/v1/calls/{call.call_id}/handoff", json={"target_agent_id": billing}
        )
        assert handed.status_code == 200, handed.text
        await call.say(400)
        await call.wait_for_agent_speech(3)  # "Thanks."
        await _events(app, call.call_id, "skill.loaded", 1)
        await call.hang_up()
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    [given] = after_handoff
    assert "- verify: Use for verify." in _prompt(given)
    [given] = after_load
    assert "## Skill: verify (v1)\nAsk Dana for the date of birth." in _prompt(given)
    assert given.tools == ["load_skill", "verify_tool"]
    assert read["skills"] == [
        {
            "skill": "verify",
            "version": skill["version"],
            "pinned": False,
            "loaded": True,
        }
    ]


class _Api:
    """Another project's client in the shape `_publish_skill` takes (`.api`)."""

    def __init__(self, api: Any) -> None:
        self.api = api
