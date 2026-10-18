"""Releasing a number, end to end, on Twilio and Telnyx (#321, ADR-0025).

An admin key gives a number on the carrier account back to the carrier with
`POST /v1/phone-numbers/release`. Releasing is irreversible, so it is refused
while TurnCall binds the number (409) and without `confirm` repeating the
number (422); developer and viewer keys cannot release at all. Unbinding is
not releasing: it leaves the number on the account, where it can be bound
again. Once released, the number is off the account: bind cannot find it and
a search does not list it. Driven through Seam A (the real app, the fake
carrier, whose `release_number` action is the release). The unit halves are
`tests/unit/test_phone_number_release.py` and the adapter tests in
`test_twilio_number_purchase.py` / `test_telnyx_number_purchase.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, run_app

pytestmark = pytest.mark.integration

CARRIERS = ["twilio", "telnyx"]
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


async def _buy(app: Harness, carrier: str, e164: str) -> dict[str, Any]:
    """Put `e164` on the carrier account the way an admin would: buy it."""
    app.carrier.offer(e164)
    bought = await app.api.post(
        "/v1/phone-numbers/purchase", json={"carrier": carrier, "e164_number": e164}
    )
    assert bought.status_code == 201, bought.text
    return bought.json()["data"]


async def _bind(app: Harness, number: dict[str, Any], agent_id: str) -> Any:
    return await app.api.post(
        "/v1/phone-numbers",
        json={
            "carrier": number["carrier"],
            "e164_number": number["e164_number"],
            "external_number_sid": number["external_number_sid"],
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
        },
    )


async def _bind_looked_up(app: Harness, number: dict[str, Any], agent_id: str) -> Any:
    """Bind the number the way that looks it up on the carrier account: by
    E.164 on Telnyx, and on Twilio with an account pair (bind trusts a PN SID
    on the platform's account without asking Twilio)."""
    own = (
        {"twilio_account_sid": OWN_SID, "twilio_auth_token": OWN_TOKEN}
        if number["carrier"] == "twilio"
        else {}
    )
    return await app.api.post(
        "/v1/phone-numbers",
        json={
            "carrier": number["carrier"],
            "e164_number": number["e164_number"],
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
            **own,
        },
    )


def _release(carrier: str, e164: str, **confirm: Any) -> dict[str, Any]:
    return {"carrier": carrier, "e164_number": e164, **confirm}


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_refused_while_bound_and_released_after_unbind(
    monkeypatch, carrier: str
) -> None:
    e164 = "+13125550160"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        number = await _buy(app, carrier, e164)
        bound = await _bind(app, number, agent_id)
        assert bound.status_code == 201, bound.text
        binding_id = bound.json()["data"]["id"]

        while_bound = await app.api.post(
            "/v1/phone-numbers/release", json=_release(carrier, e164, confirm=e164)
        )
        owned_while_bound = app.carrier.owns(e164)

        unbound = await app.api.delete(f"/v1/phone-numbers/{binding_id}")
        assert unbound.status_code == 200, unbound.text
        owned_after_unbind = app.carrier.owns(e164)

        released = await app.api.post(
            "/v1/phone-numbers/release", json=_release(carrier, e164, confirm=e164)
        )
        again = await app.api.post(
            "/v1/phone-numbers/release", json=_release(carrier, e164, confirm=e164)
        )
        rebound = await _bind_looked_up(app, number, agent_id)
        searched = await app.api.get(
            "/v1/phone-numbers/available",
            params={"carrier": carrier, "area_code": "312"},
        )
        log = await _log(app)

    # Bound: refused, naming the binding to delete first; nothing released.
    assert while_bound.status_code == 409, while_bound.text
    assert while_bound.json()["details"] == {"phone_number_id": binding_id}
    assert owned_while_bound
    # Unbinding left it on the account; the release took it off.
    assert owned_after_unbind
    assert released.status_code == 200, released.text
    assert released.json()["data"] == {
        "carrier": carrier,
        "e164_number": e164,
        "external_number_sid": number["external_number_sid"],
        "status": "released",
        "twilio_account_sid": None,
        "twilio_credentials": "platform" if carrier == "twilio" else None,
    }
    [release] = app.carrier.actions_of("release_number")
    assert release.params == {
        "number_sid": number["external_number_sid"],
        "e164": e164,
    }
    expected_account = (
        app.carrier.account_sid if carrier == "twilio" else app.carrier.telnyx_api_key
    )
    assert release.account_sid == expected_account
    # Gone from the account: neither a second release nor a bind finds it,
    # and it is not back on sale.
    assert not app.carrier.owns(e164)
    assert again.status_code == 422, again.text
    assert "not a phone number on" in again.json()["error"]
    assert len(app.carrier.actions_of("release_number")) == 1
    assert rebound.status_code == 422, rebound.text
    assert "not a phone number on" in rebound.json()["error"]
    assert searched.status_code == 200, searched.text
    assert searched.json()["data"] == []
    # Audited by the number, after the unbind.
    entry = next(e for e in log if e["action"] == "released")
    assert entry["id"] == released.headers["X-TurnCall-Audit-Id"]
    assert entry["resource_type"] == "phone_number"
    assert entry["resource_id"] == e164
    assert entry["changes"]["/e164_number"] == {"before": e164, "after": None}
    assert [e["action"] for e in log][:2] == ["released", "deleted"]


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_refused_without_confirmation(monkeypatch, carrier: str) -> None:
    e164 = "+13125550161"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _buy(app, carrier, e164)
        log_before = await _log(app)

        refused = [
            await app.api.post("/v1/phone-numbers/release", json=body)
            for body in (
                _release(carrier, e164),
                _release(carrier, e164, confirm=True),
                _release(carrier, e164, confirm="+13125550199"),
            )
        ]
        log_after = await _log(app)

    for response in refused:
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "validation_error"
    assert app.carrier.actions_of("release_number") == []
    assert app.carrier.owns(e164)
    assert log_after == log_before


@pytest.mark.parametrize("role", ["developer", "viewer"])
@pytest.mark.parametrize("carrier", CARRIERS)
async def test_only_an_admin_key_releases(monkeypatch, carrier: str, role: str) -> None:
    e164 = "+13125550162"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _buy(app, carrier, e164)
        headers = await _headers(app, role)
        log_before = await _log(app)

        refused = await app.api.post(
            "/v1/phone-numbers/release",
            json=_release(carrier, e164, confirm=e164),
            headers=headers,
        )
        log_after = await _log(app)

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "forbidden"
    assert app.carrier.actions_of("release_number") == []
    assert app.carrier.owns(e164)
    assert log_after == log_before


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_unbinding_never_releases(monkeypatch, carrier: str) -> None:
    """Unbind is unchanged by release: it deletes the binding and points the
    number away from TurnCall; the number stays on the account and binds
    again."""
    e164 = "+13125550163"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        number = await _buy(app, carrier, e164)
        bound = await _bind(app, number, agent_id)
        assert bound.status_code == 201, bound.text

        unbound = await app.api.delete(
            f"/v1/phone-numbers/{bound.json()['data']['id']}"
        )
        voice_url_after_unbind = app.carrier.number(e164).voice_url
        rebound = await _bind(app, number, agent_id)

    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["data"] == {"deleted": True}
    assert app.carrier.actions_of("release_number") == []
    # Off TurnCall, still on the account.
    assert voice_url_after_unbind == ""
    assert rebound.status_code == 201, rebound.text
    assert app.carrier.number(e164).sid == number["external_number_sid"]
