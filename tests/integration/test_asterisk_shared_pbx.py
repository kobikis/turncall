"""One PBX credential held by several projects (#274).

The builder mints a project per agent and gives each the workspace's
`asterisk` credential, so one PBX is several projects' Asterisk. ARI hands an
app's events to the newest socket only: an edge per project left every
project's numbers but the last one's unanswered. The edge is per account.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.harness.app import Harness, _bootstrap, _delete_project, _settle
from tests.harness.asterisk import FakeAsterisk

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello from Asterisk."}


@asynccontextmanager
async def _another_project(app: Harness) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=app.base_url, timeout=10) as http:
        project_id, api_key = await _bootstrap(http)
        http.headers["Authorization"] = f"Bearer {api_key}"
        try:
            yield http
        finally:
            # The app is still serving: a call it has not finished closing
            # would race the delete's cascade into a deadlock.
            await _settle()
            await _delete_project(project_id)


async def _bind_on(api: httpx.AsyncClient, pbx: FakeAsterisk) -> str:
    """The PBX as this project's own credential, and a number on it."""
    from turncall.webhooks.asterisk_edge import EDGES

    stored = await api.put(
        "/v1/credentials/asterisk",
        json={"ari_url": pbx.url, "username": pbx.username, "password": pbx.password},
    )
    assert stored.status_code == 200, stored.text
    await EDGES.reconcile()  # the poke's, awaited
    agent = await api.post("/v1/agents", json={"name": "b", "config": AGENT})
    assert agent.status_code == 201, agent.text
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    bound = await api.post(
        "/v1/phone-numbers",
        json={
            "carrier": "asterisk",
            "e164_number": e164,
            "routing_target_type": "agent",
            "routing_target_id": agent.json()["data"]["id"],
        },
    )
    assert bound.status_code == 201, bound.text
    await asyncio.wait_for(pbx.connected.wait(), 5)
    return e164


async def test_projects_sharing_a_pbx_each_get_their_numbers_answered(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        first = await app.bind_asterisk_number(await app.create_agent(AGENT))
        async with _another_project(app) as other:
            second = await _bind_on(other, app.asterisk)
            for number in (second, first):
                call = await app.asterisk.ring("+15550001234", number)
                await call.hang_up()

    assert app.asterisk.event_connections == 1
    assert not app.carrier.actions_of("continue")


async def test_a_number_dialled_on_another_projects_pbx_is_refused(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await app.bind_asterisk_number(await app.create_agent(AGENT))
        pbx = FakeAsterisk(
            app.carrier, app.base_url.replace("http", "ws") + "/ws/asterisk-media"
        )
        await pbx.start()
        try:
            async with _another_project(app) as other:
                number = await _bind_on(other, pbx)
                stray = await app.asterisk.ring(
                    "+15550001234", number, until_bridged=False
                )
                await asyncio.wait_for(stray.closed.wait(), 5)
                call = await pbx.ring("+15550001234", number)
                await call.hang_up()
        finally:
            await pbx.stop()

    assert [a.params["call_sid"] for a in app.carrier.actions_of("continue")] == [
        stray.call_sid
    ]
    assert (app.asterisk.event_connections, pbx.event_connections) == (1, 1)
