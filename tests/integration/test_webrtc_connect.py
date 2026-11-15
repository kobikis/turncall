"""POST /v1/webrtc/connect, opened like every voice call (#494).

The call is opened for real — slot, call-init, the row and its events — and
run through `run_call`; only pipecat's side is faked: the peer the request
negotiates, and the pipeline its run would build, which is recorded and left
running until the test ends (or raises, for a build that fails).

A browser call's `{{variables}}`: a console's test call has no call-init of
its own to send them. The request's win a clash with call-init's, as an
outbound dial's do; call-init is shown them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.test_text_session_variables import CallInitServer
from turncall.storage.database import get_session_factory
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration

VARIABLES = {"name": "Ada", "vip": True}


class FakeConnection:
    """What the endpoint touches on pipecat's connection."""

    pc_id = "pc"

    def __init__(self) -> None:
        self.disconnect = AsyncMock()
        self.closed: list = []

    def event_handler(self, name: str):
        def register(handler):
            if name == "closed":
                self.closed.append(handler)
            return handler

        return register


class _Running:
    def __init__(self, over: asyncio.Event) -> None:
        self._over = over

    async def start(self) -> None:
        await self._over.wait()


@dataclass
class Browser:
    """`started`: each build's (config, call context). `fails`: what the
    next build raises instead. `connection`: the peer the request made."""

    started: list[Any] = field(default_factory=list)
    fails: Exception | None = None
    connection: FakeConnection | None = None


@pytest.fixture
async def browser(monkeypatch) -> AsyncIterator[Browser]:
    from turncall.api.v1 import webrtc
    from turncall.orchestrator import call_runner

    leg, over = Browser(), asyncio.Event()

    async def handle(_request: Any, callback: Any) -> dict:
        leg.connection = FakeConnection()
        # What pipecat's handler does: log the callback's error, answer anyway.
        with contextlib.suppress(Exception):
            await callback(leg.connection)
        return {"sdp": "v=0", "type": "answer", "pc_id": "pc"}

    async def build(*, agent: Any, call_context: Any, **_kwargs: Any) -> _Running:
        if leg.fails is not None:
            raise leg.fails
        leg.started.append((agent.config, call_context))
        return _Running(over)

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", handle)
    monkeypatch.setattr(webrtc, "create_whatsapp_transport", lambda *a, **k: None)
    monkeypatch.setattr(call_runner, "build_call_pipeline", build)
    yield leg
    over.set()
    await asyncio.gather(*list(call_runner._RUNNING), return_exceptions=True)


async def _connect(app: Any, **request: Any) -> Any:
    return await app.api.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "requestData": request},
    )


async def _template_variables(call_id: str) -> dict:
    async with get_session_factory()() as session:
        row = await call_repo.get_call_by_id(session, UUID(call_id))
    return row.metadata_json.get("template_variables", {})


async def test_a_pipeline_that_cannot_start_is_an_error_not_a_dead_answer(
    monkeypatch, browser
) -> None:
    """pipecat answers the offer even when the connection callback raised, so
    the browser connected to a peer with nothing behind it. The build failure
    has to come back as an error, and the half-made peer has to be closed."""
    browser.fails = RuntimeError("Missing credentials")
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        r = await _connect(app, agent_id=agent_id)
        calls = (await app.api.get("/v1/calls")).json()["data"]

    assert r.status_code == 500, r.text
    assert r.json()["code"] == "pipeline_start_failed"
    assert "Missing credentials" in r.json()["error"]
    [call] = calls
    assert call["id"] in r.json()["error"]
    assert browser.connection is not None
    browser.connection.disconnect.assert_awaited_once()


async def test_a_missing_key_rides_back_as_a_list(monkeypatch, browser) -> None:
    """So a console can name the key without parsing the sentence."""
    from turncall.services.credentials import MissingProviderKeyError

    browser.fails = MissingProviderKeyError(["deepgram"])
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        r = await _connect(app, agent_id=await app.create_agent(HARNESS_AGENT))

    assert r.status_code == 500, r.text
    assert r.json()["details"] == {"missing_keys": ["deepgram"]}
    assert "No API key for deepgram" in r.json()["error"]


async def test_the_requests_variables_render_the_agent(monkeypatch, browser) -> None:
    agent = {**HARNESS_AGENT, "system_prompt": "Hi {{name}}, vip={{vip}}."}
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(agent)
        r = await _connect(app, agent_id=agent_id, variables=VARIABLES)
        assert r.status_code == 200, r.text
        stored = await _template_variables(r.json()["call_id"])

    [(config, _)] = browser.started
    assert config.system_prompt.startswith("Hi Ada, vip=True.")
    # On the call row, where a handoff and post-call work read them.
    assert stored == {"name": "Ada", "vip": "True"}


async def test_call_init_is_asked_with_the_calls_id_and_shown_them(
    monkeypatch, browser
) -> None:
    async with (
        CallInitServer() as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        server.answer = {"agent_id": await app.create_agent(HARNESS_AGENT)}
        r = await _connect(app, server_url=server.url, variables={"name": "Ada"})

    assert r.status_code == 200, r.text
    [asked] = server.requests
    # The id the call is then created with, not a stand-in.
    assert asked["call"]["id"] == r.json()["call_id"]
    assert asked["call"]["variables"] == {"name": "Ada"}


async def test_they_win_a_clash_with_call_inits(monkeypatch, browser) -> None:
    inline = {**HARNESS_AGENT, "system_prompt": "{{name}} calls from {{caller}}."}
    async with (
        CallInitServer(
            {"agent": inline, "variables": {"name": "Bob", "caller": "+1555"}}
        ) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        r = await _connect(app, server_url=server.url, variables={"name": "Ada"})
        assert r.status_code == 200, r.text
        stored = await _template_variables(r.json()["call_id"])

    [(config, _)] = browser.started
    assert config.system_prompt.startswith("Ada calls from +1555.")
    assert stored == {"name": "Ada", "caller": "+1555"}


async def test_a_started_call_is_held_until_its_connection_closes(
    monkeypatch, browser
) -> None:
    from turncall.api.v1 import webrtc

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        r = await _connect(app, agent_id=await app.create_agent(HARNESS_AGENT))
        assert r.status_code == 200, r.text
        held = webrtc._calls_by_pc["pc"][0]
        assert browser.connection is not None
        for closed in browser.connection.closed:
            await closed(browser.connection)

    assert str(held) == r.json()["call_id"]
    assert "pc" not in webrtc._calls_by_pc
