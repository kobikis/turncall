"""Searching for, buying and binding a number, end to end (#319, ADR-0025).

An admin key searches the carrier account for numbers, buys one, binds it
with what the buy returned, and a call to it reaches the agent. Developer and
viewer keys can do neither: buying spends the operator's money. Driven
through Seam A (the real app, stub providers, the fake carrier, whose
`offer` puts numbers up for sale and whose `buy_number` action is the
purchase). The unit halves are `tests/unit/test_phone_number_purchase.py`
and `test_twilio_number_purchase.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, place_inbound_call, run_app

pytestmark = pytest.mark.integration

OWN_SID = "AC" + "c" * 32
OWN_TOKEN = "own-twilio-auth-token"


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


async def _search(app: Harness, **params: Any) -> list[dict[str, Any]]:
    response = await app.api.get("/v1/phone-numbers/available", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_search_buy_bind_and_a_call_reaches_the_agent(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550142")
        app.carrier.offer("+14155550142")
        agent_id = await app.create_agent(
            {**HARNESS_AGENT, "first_message": "Hello on your new number."}
        )

        found = await _search(app, country="US", area_code="312", contains="0142")
        assert [n["e164_number"] for n in found] == ["+13125550142"]
        assert found[0]["carrier"] == "twilio"
        assert found[0]["capabilities"]["voice"] is True

        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"carrier": "twilio", "e164_number": "+13125550142"},
        )
        assert bought.status_code == 201, bought.text
        number = bought.json()["data"]
        purchase_entry = bought.headers["X-TurnCall-Audit-Id"]

        bound = await app.api.post(
            "/v1/phone-numbers",
            json={
                "carrier": number["carrier"],
                "e164_number": number["e164_number"],
                "external_number_sid": number["external_number_sid"],
                "routing_target_type": "agent",
                "routing_target_id": agent_id,
            },
        )
        assert bound.status_code == 201, bound.text
        assert bound.json()["data"]["webhooks_configured"] is True

        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number="+13125550142"
        )
        await call.wait_for_agent_speech(1)  # the greeting: the agent answered
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

        still_for_sale = await _search(app, country="US", area_code="312")
        log = await _log(app)

    # Bought on the platform's Twilio account: one purchase, of that number.
    [buy] = app.carrier.actions_of("buy_number")
    assert buy.account_sid == app.carrier.account_sid
    assert buy.params == {
        "e164": "+13125550142",
        "number_sid": number["external_number_sid"],
    }
    assert number == {
        "carrier": "twilio",
        "e164_number": "+13125550142",
        "external_number_sid": app.carrier.number("+13125550142").sid,
        "friendly_name": "+13125550142",
        "status": "active",
        "twilio_account_sid": None,
        "twilio_credentials": "platform",
    }
    # Sold: off the shelf, and the bind pointed it at TurnCall.
    assert still_for_sale == []
    assert app.carrier.number("+13125550142").voice_url.endswith(
        "/webhooks/twilio/voice/inbound"
    )
    assert ended["call_id"] == call.call_id
    assert ended["payload"]["status"] == "completed"
    # The purchase is in the audit log, by the number, before its binding.
    purchased = next(e for e in log if e["action"] == "purchased")
    assert purchased["id"] == purchase_entry
    assert purchased["resource_type"] == "phone_number"
    assert purchased["resource_id"] == "+13125550142"
    assert [e["action"] for e in log][:2] == ["created", "purchased"]


async def test_a_number_bought_on_its_own_twilio_account_binds_on_it(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550143")
        agent_id = await app.create_agent(HARNESS_AGENT)
        own = {"twilio_account_sid": OWN_SID, "twilio_auth_token": OWN_TOKEN}

        searched = await app.api.get(
            "/v1/phone-numbers/available",
            params={"area_code": "312"},
            headers={
                "X-Twilio-Account-Sid": OWN_SID,
                "X-Twilio-Auth-Token": OWN_TOKEN,
            },
        )
        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"e164_number": "+13125550143", **own},
        )
        number = bought.json()["data"]
        bound = await app.api.post(
            "/v1/phone-numbers",
            json={
                "e164_number": number["e164_number"],
                "routing_target_type": "agent",
                "routing_target_id": agent_id,
                **own,
            },
        )

    assert searched.status_code == 200, searched.text
    assert bought.status_code == 201, bought.text
    assert OWN_TOKEN not in bought.text
    assert number["twilio_account_sid"] == OWN_SID
    assert number["twilio_credentials"] == "number"
    [buy] = app.carrier.actions_of("buy_number")
    assert buy.account_sid == OWN_SID
    assert bound.status_code == 201, bound.text
    data = bound.json()["data"]
    assert data["external_number_sid"] == number["external_number_sid"]
    assert data["twilio_account_sid"] == OWN_SID


@pytest.mark.parametrize("role", ["developer", "viewer"])
async def test_only_an_admin_key_searches_or_buys(monkeypatch, role: str) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.offer("+13125550144")
        headers = await _headers(app, role)
        log_before = await _log(app)

        searched = await app.api.get(
            "/v1/phone-numbers/available", params={"area_code": "312"}, headers=headers
        )
        bought = await app.api.post(
            "/v1/phone-numbers/purchase",
            json={"e164_number": "+13125550144"},
            headers=headers,
        )
        log_after = await _log(app)

    for refused in (searched, bought):
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
    # Nothing bought, nothing recorded: the number is still for sale.
    assert app.carrier.actions_of("buy_number") == []
    assert [n.e164 for n in app.carrier.for_sale()] == ["+13125550144"]
    assert log_after == log_before


async def test_a_number_not_for_sale_is_refused_and_unrecorded(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        log_before = await _log(app)
        bought = await app.api.post(
            "/v1/phone-numbers/purchase", json={"e164_number": "+13125550145"}
        )
        log_after = await _log(app)

    assert bought.status_code == 422, bought.text
    assert bought.json()["details"] == {"twilio_code": 21422}
    assert app.carrier.actions_of("buy_number") == []
    assert log_after == log_before
