"""Releasing a number, end to end, on Twilio and Telnyx (#321, ADR-0025).

An admin key gives a number on the carrier account back to the carrier with
`POST /v1/phone-numbers/release`. Releasing is irreversible, so it is refused
while TurnCall binds the number (409) and without `confirm` repeating the
number (422); developer and viewer keys cannot release at all. Unbinding is
not releasing: it leaves the number on the account, where it can be bound
again. Once released, the number is off the account: bind cannot find it and
a search does not list it. On the platform's shared account (Twilio without
an account pair, Telnyx without the project's `telnyx` credential) only a
number this project bought through the API is released (403 otherwise); the
project's own account is not restricted. Driven through Seam A (the real app,
the fake carrier, whose `release_number` action is the release). The unit halves are
`tests/unit/test_phone_number_release.py` and the adapter tests in
`test_twilio_number_purchase.py` / `test_telnyx_number_purchase.py`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, run_app
from tests.harness.app import PLATFORM_KEY

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
    # Gone from the account, and no longer the project's: a second release
    # on the platform's account is refused before the carrier is asked, a
    # bind does not find it, and it is not back on sale.
    assert not app.carrier.owns(e164)
    assert again.status_code == 403, again.text
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


# --- the platform's shared account -------------------------------------------


async def _other_project(app: Harness) -> httpx.AsyncClient:
    """An admin client of a second project, bootstrapped as the builder does."""
    platform = {"X-Platform-Key": PLATFORM_KEY}
    project = await app.api.post("/v1/projects", json={"name": "B"}, headers=platform)
    assert project.status_code == 201, project.text
    key = await app.api.post(
        "/v1/api-keys",
        params={"project_id": project.json()["data"]["id"]},
        json={"name": "b", "role": "admin"},
        headers=platform,
    )
    assert key.status_code == 201, key.text
    token = key.json()["data"]["raw_key"]
    return httpx.AsyncClient(
        base_url=app.base_url, headers={"Authorization": f"Bearer {token}"}
    )


def _refused_as_not_bought(response: Any) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "forbidden"
    assert body["details"] == {"account": "platform"}


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_a_platform_number_the_project_did_not_buy_is_refused(
    monkeypatch, carrier: str
) -> None:
    """A number on the platform's account that no purchase put there (the
    operator's, bought by hand) is not this project's to release."""
    e164 = "+13125550164"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.add_number(e164)
        log_before = await _log(app)
        refused = await app.api.post(
            "/v1/phone-numbers/release", json=_release(carrier, e164, confirm=e164)
        )
        log_after = await _log(app)

    _refused_as_not_bought(refused)
    assert app.carrier.actions_of("release_number") == []
    assert app.carrier.owns(e164)
    assert log_after == log_before


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_another_projects_purchase_is_refused(monkeypatch, carrier: str) -> None:
    e164 = "+13125550165"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _buy(app, carrier, e164)
        async with await _other_project(app) as other:
            by_other = await other.post(
                "/v1/phone-numbers/release",
                json=_release(carrier, e164, confirm=e164),
            )
        owned_after_refusal = app.carrier.owns(e164)
        by_buyer = await app.api.post(
            "/v1/phone-numbers/release", json=_release(carrier, e164, confirm=e164)
        )

    _refused_as_not_bought(by_other)
    assert owned_after_refusal
    # The project that bought it still can.
    assert by_buyer.status_code == 200, by_buyer.text
    assert len(app.carrier.actions_of("release_number")) == 1
    assert not app.carrier.owns(e164)


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_a_number_bought_again_belongs_to_its_new_buyer(
    monkeypatch, carrier: str
) -> None:
    """A number that left the platform's account outside TurnCall and was
    bought again by another project is the new buyer's: the first buyer's
    record of it no longer lets it release the number."""
    e164 = "+13125550166"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        first = await _buy(app, carrier, e164)
        # Released on the carrier's console, then up for sale again.
        app.carrier._release(app.carrier.number(e164).sid)
        app.carrier.offer(e164)
        async with await _other_project(app) as other:
            bought = await other.post(
                "/v1/phone-numbers/purchase",
                json={"carrier": carrier, "e164_number": e164},
            )
            by_first = await app.api.post(
                "/v1/phone-numbers/release",
                json=_release(carrier, e164, confirm=e164),
            )
            owned_after_refusal = app.carrier.owns(e164)
            by_new_buyer = await other.post(
                "/v1/phone-numbers/release",
                json=_release(carrier, e164, confirm=e164),
            )

    assert first["e164_number"] == e164
    assert bought.status_code == 201, bought.text
    _refused_as_not_bought(by_first)
    assert owned_after_refusal
    assert by_new_buyer.status_code == 200, by_new_buyer.text
    assert not app.carrier.owns(e164)


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_a_release_on_the_projects_own_account_is_unrestricted(
    monkeypatch, carrier: str
) -> None:
    """On the project's own account (a Twilio pair in the body, or the
    project's `telnyx` credential) any number on it can be released, bought
    through TurnCall or not."""
    e164 = "+13125550167"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        app.carrier.add_number(e164)  # not bought through the API
        if carrier == "telnyx":
            stored = await app.api.put(
                "/v1/credentials/telnyx",
                json={
                    "api_key": "KEY-project",
                    "public_key": app.carrier.telnyx_public_key,
                },
            )
            assert stored.status_code == 200, stored.text
            own: dict[str, Any] = {}
        else:
            own = {"twilio_account_sid": OWN_SID, "twilio_auth_token": OWN_TOKEN}
        released = await app.api.post(
            "/v1/phone-numbers/release",
            json=_release(carrier, e164, confirm=e164, **own),
        )

    assert released.status_code == 200, released.text
    [release] = app.carrier.actions_of("release_number")
    assert release.account_sid == (OWN_SID if carrier == "twilio" else "KEY-project")
    assert not app.carrier.owns(e164)
