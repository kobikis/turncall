"""Searching for, buying and binding a Telnyx number, end to end (#320,
ADR-0025).

The same endpoints as Twilio's (`test_harness_number_purchase.py`), with
`carrier: telnyx`, on the project's Telnyx account (here the platform's
TELNYX_API_KEY, which a project `telnyx` credential would replace). A bought
number binds onto this deployment's Call Control application like any other,
and a call to it reaches the agent. A Telnyx purchase is a number order: one
Telnyx has not finished is answered "pending", bind refuses the number until
it lands on the account, and binds it after. Developer and viewer keys can do
none of it. Driven through Seam A; the adapter half is
`tests/unit/test_telnyx_number_purchase.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, run_app, telnyx

pytestmark = pytest.mark.integration


async def _headers(app: Harness, role: str) -> dict[str, str]:
    created = await app.api.post(
        "/v1/api-keys/authenticated", json={"name": role, "role": role}
    )
    assert created.status_code == 201, created.text
    return {"Authorization": f"Bearer {created.json()['data']['raw_key']}"}


async def _log(app: Harness) -> list[dict[str, Any]]:
    response = await app.api.get("/v1/audit-log")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _bind(number: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """What the purchase returned, as bind takes it."""
    return {
        "carrier": number["carrier"],
        "e164_number": number["e164_number"],
        "external_number_sid": number["external_number_sid"],
        "routing_target_type": "agent",
        "routing_target_id": agent_id,
    }


async def test_search_buy_bind_and_a_call_reaches_the_agent(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550152")
        app.carrier.offer("+14155550152")
        agent_id = await app.create_agent(
            {**HARNESS_AGENT, "first_message": "Hello on your Telnyx number."}
        )

        searched = await app.api.get(
            "/v1/phone-numbers/available",
            params={"carrier": "telnyx", "area_code": "312", "contains": "0152"},
        )
        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"carrier": "telnyx", "e164_number": "+13125550152"},
        )
        number = bought.json()["data"]
        bound = await app.api.post("/v1/phone-numbers", json=_bind(number, agent_id))

        call = await telnyx.place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number="+13125550152"
        )
        await call.wait_for_agent_speech(1)  # the greeting: the agent answered
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        log = await _log(app)

    assert searched.status_code == 200, searched.text
    [found] = searched.json()["data"]
    assert found["carrier"] == "telnyx"
    assert found["e164_number"] == "+13125550152"
    assert found["monthly_cost"] == "1.00"

    assert bought.status_code == 201, bought.text
    owned = app.carrier.number("+13125550152")
    [buy] = app.carrier.actions_of("buy_number")
    # Bought on the Telnyx account, by its API key.
    assert buy.account_sid == app.carrier.telnyx_api_key
    assert buy.params["number_sid"] == owned.sid
    assert number == {
        "carrier": "telnyx",
        "e164_number": "+13125550152",
        "external_number_sid": owned.sid,
        "friendly_name": None,
        "status": "active",
        "twilio_account_sid": None,
        "twilio_credentials": None,
        "telnyx_order_id": buy.params["order_id"],
    }

    # Bound onto this deployment's Call Control application like any other.
    assert bound.status_code == 201, bound.text
    assert bound.json()["data"]["external_number_sid"] == owned.sid
    assert bound.json()["data"]["webhooks_configured"] is True
    assert owned.voice_url == f"{app.base_url}/webhooks/telnyx/voice"
    assert owned.connection_id.startswith("TurnCall ")
    assert ended["call_id"] == call.call_id
    assert app.carrier.for_sale()[0].e164 == "+14155550152"

    purchased = next(e for e in log if e["action"] == "purchased")
    assert purchased["id"] == bought.headers["X-TurnCall-Audit-Id"]
    assert purchased["resource_type"] == "phone_number"
    assert purchased["resource_id"] == "+13125550152"


async def test_a_pending_order_binds_once_telnyx_finishes_it(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550153")
        app.carrier.hold_orders = True
        agent_id = await app.create_agent(HARNESS_AGENT)

        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"carrier": "telnyx", "e164_number": "+13125550153"},
        )
        number = bought.json()["data"]
        too_soon = await app.api.post("/v1/phone-numbers", json=_bind(number, agent_id))
        [landed] = app.carrier.finish_orders()
        bound = await app.api.post("/v1/phone-numbers", json=_bind(number, agent_id))
        log = await _log(app)

    assert bought.status_code == 201, bought.text
    assert number["status"] == "pending"
    assert number["external_number_sid"] is None
    assert number["telnyx_order_id"]
    # Ordered is bought: audited even though the number is not on the account yet.
    purchased = next(e for e in log if e["action"] == "purchased")
    assert purchased["resource_id"] == "+13125550153"
    # Not on the account yet: bind cannot find it.
    assert too_soon.status_code == 422, too_soon.text
    # Landed: bind looks it up by its E.164 and stores its Telnyx id.
    assert bound.status_code == 201, bound.text
    assert bound.json()["data"]["external_number_sid"] == landed.sid
    assert landed.voice_url.endswith("/webhooks/telnyx/voice")


@pytest.mark.parametrize("role", ["developer", "viewer"])
async def test_only_an_admin_key_searches_or_buys(monkeypatch, role: str) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550154")
        headers = await _headers(app, role)
        log_before = await _log(app)

        searched = await app.api.get(
            "/v1/phone-numbers/available",
            params={"carrier": "telnyx"},
            headers=headers,
        )
        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"carrier": "telnyx", "e164_number": "+13125550154"},
            headers=headers,
        )
        log_after = await _log(app)

    for refused in (searched, bought):
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
    assert app.carrier.actions_of("buy_number") == []
    assert [n.e164 for n in app.carrier.for_sale()] == ["+13125550154"]
    assert log_after == log_before


async def test_a_number_not_for_sale_is_refused_and_unrecorded(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        log_before = await _log(app)
        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"carrier": "telnyx", "e164_number": "+13125550155"},
        )
        log_after = await _log(app)

    assert bought.status_code == 422, bought.text
    assert bought.json()["details"] == {"telnyx_codes": ["85001"]}
    assert app.carrier.actions_of("buy_number") == []
    assert log_after == log_before
