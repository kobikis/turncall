"""What the call-init project-scope tests share (#475): the prompts that
tell the number's own agent from another project's, and the numbers they
ring."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from tests.harness import HARNESS_AGENT, Script
from tests.integration.test_session_message_rate import _enable_whatsapp

CALLER = "+15550009999"
OWN_PROMPT = "The number's own agent."
FOREIGN_PROMPT = "Another project's agent."
SCRIPT = Script(agent=("Hello there.",))


async def foreign_agent(app: Any, stack: Any) -> str:
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


async def route(app: Any, carrier: str, server_url: str, **extra: Any) -> str:
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


async def calls(app: Any) -> list[dict[str, Any]]:
    return (await app.api.get("/v1/calls")).json()["data"]


async def whatsapp_number(app: Any, server_url: str) -> str:
    """A fresh Twilio number routed to `server_url`, WhatsApp enabled."""
    number = await route(app, "twilio", server_url)
    await _enable_whatsapp(number)
    return number
