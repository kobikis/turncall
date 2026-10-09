"""Binding a Telnyx number routes it to TurnCall (#219), through the running
app: no console step between the bind and the first call.

Driven through Seam A — the fake carrier owns the numbers and records the
routing the app asks for. What that routing is on Telnyx's side (this
deployment's Call Control application, created, reused, deleted when empty)
is the adapter's, pinned in `tests/unit/test_telnyx_number_routing.py` and
against the real API in `tests/live/test_live_telnyx.py`.
"""

from __future__ import annotations

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app, telnyx

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello from Telnyx."}


async def test_a_bound_number_is_routed_and_answers(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(AGENT)
        number = await telnyx.bind_number(app.api, app.carrier, agent_id)
        [bound] = (await app.api.get("/v1/phone-numbers")).json()["data"]
        routed = app.carrier.number(number)
        # The harness rings the number at the URL binding routed it to.
        call = await telnyx.place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    # Looked up on the account: the number's Telnyx id, without being told.
    assert bound["external_number_sid"] == routed.sid
    assert routed.voice_url == f"{app.base_url}/webhooks/telnyx/voice"
    assert [
        a.params["number_sid"] for a in app.carrier.actions_of("configure_number")
    ] == [routed.sid]


async def test_a_number_the_account_cannot_see_is_refused(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        response = await app.api.post(
            "/v1/phone-numbers",
            json={
                "carrier": "telnyx",
                "e164_number": "+15550009999",  # never added to the carrier
                "routing_target_type": "agent",
                "routing_target_id": await app.create_agent(AGENT),
            },
        )
        listed = (await app.api.get("/v1/phone-numbers")).json()["data"]

    assert response.status_code == 422, response.text
    assert response.json()["error"] == (
        "+15550009999 is not a phone number on this project's Telnyx account"
    )
    assert listed == []
    assert app.carrier.actions_of("configure_number") == []


async def test_no_telnyx_key_anywhere_is_named(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        monkeypatch.setenv("TELNYX_API_KEY", "")
        from turncall.config import get_settings

        get_settings.cache_clear()
        app.carrier.add_number("+15550008888")
        response = await app.api.post(
            "/v1/phone-numbers",
            json={
                "carrier": "telnyx",
                "e164_number": "+15550008888",
                "routing_target_type": "agent",
                "routing_target_id": await app.create_agent(AGENT),
            },
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "no_telnyx_account"
    assert response.json()["details"] == {"missing_keys": ["telnyx"]}


async def test_update_reroutes_and_unbind_takes_the_number_off(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(AGENT)
        number = await telnyx.bind_number(app.api, app.carrier, agent_id)
        [bound] = (await app.api.get("/v1/phone-numbers")).json()["data"]
        # Taken off by hand in the console: an update is the recovery path.
        app.carrier.number(number).voice_url = ""
        updated = await app.api.put(
            f"/v1/phone-numbers/{bound['id']}",
            json={"routing_target_type": "agent", "routing_target_id": agent_id},
        )
        rerouted = app.carrier.number(number).voice_url
        deleted = await app.api.delete(f"/v1/phone-numbers/{bound['id']}")

    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["webhooks_configured"] is True
    assert rerouted.endswith("/webhooks/telnyx/voice")
    assert deleted.status_code == 200, deleted.text
    routing = [
        a.params["voice_url"] for a in app.carrier.actions_of("configure_number")
    ]
    assert routing[-1] == ""  # unrouted on unbind
    assert app.carrier.number(number).connection_id == ""
