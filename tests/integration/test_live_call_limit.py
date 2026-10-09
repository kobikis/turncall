"""A project's live-call limit, through the running app (#224).

Driven the way a carrier and an integrator see it: an inbound call over the
limit is refused busy at the carrier and announced as `call.rejected`, never
answered; an outbound one is a 429 and the carrier never hears of it. The
count lives in Redis, so a second server process sees the same one — and every
way a call ends gives its slot back, or a project would be locked out for good.
"""

import uuid

import pytest

from tests.harness import place_inbound_call, post_signed, run_app
from tests.harness.second_instance import second_instance
from tests.integration.limits_support import SCRIPT, dial_body, limited_project, ring
from turncall.services import limits

pytestmark = pytest.mark.integration

BUSY = '<Response><Reject reason="busy"/></Response>'


async def _until_dialled(api, body: dict, *, attempts: int = 50) -> None:
    """A dial is admitted again once the slot is back; poll briefly for it."""
    import asyncio

    for _ in range(attempts):
        r = await api.post("/v1/calls/outbound", json=body)
        if r.status_code == 201:
            return
        assert r.status_code == 429, r.text
        await asyncio.sleep(0.1)
    raise AssertionError("the slot never came back")


async def test_over_the_limit_inbound_is_busy_and_outbound_is_429(
    monkeypatch, redis_env
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, number, number_id = await limited_project(app, live_calls=1)
        voice_url = app.carrier.number(number).voice_url

        live = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )

        inbound = ring(app.carrier, number)
        refused = await post_signed(app.api, app.carrier, voice_url, inbound)
        rejected = await app.sink.wait_for("call.rejected")
        over = await app.api.post(
            "/v1/calls/outbound", json=dial_body(agent_id, number_id)
        )
        calls = (await app.api.get("/v1/calls")).json()

        # Ended by the agent: the stream closes and no status callback is
        # posted, so only the media stream's own hold can give the slot back.
        await live.say(400)
        await live.closed.wait()
        await _until_dialled(app.api, dial_body(agent_id, number_id))

    assert refused.status_code == 200
    assert refused.text == BUSY
    assert rejected["_signature_valid"] is True
    assert rejected["call_id"] is None
    assert rejected["payload"]["reason"] == "live_call_limit"
    assert rejected["payload"]["limit"] == 1
    assert rejected["payload"]["provider_call_id"] == inbound["CallSid"]
    assert rejected["payload"]["transport"] == "twilio"
    # Refused before anything was written: only the live call has a row.
    assert [c["id"] for c in calls["data"]] == [live.call_id]

    assert over.status_code == 429
    assert over.json()["code"] == "rate_limited"
    assert over.json()["details"] == {"limit": "live_calls", "max": 1}
    # The one dial the carrier saw is the one made after the call ended.
    assert len(app.carrier.actions_of("dial")) == 1


async def test_the_limit_is_one_count_across_two_servers(monkeypatch, redis_env):
    async with (
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        second_instance() as other,
    ):
        other.headers["Authorization"] = app.api.headers["Authorization"]
        agent_id, number, number_id = await limited_project(app, live_calls=1)
        dial = dial_body(agent_id, number_id)

        # The other server answers a call: the project's one slot is taken.
        inbound = ring(app.carrier, number)
        answered = await post_signed(
            other,
            app.carrier,
            f"{other.base_url}/webhooks/twilio/voice/inbound",
            inbound,
        )
        here = await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).voice_url,
            ring(app.carrier, number),
        )
        refused_here = await app.api.post("/v1/calls/outbound", json=dial)
        refused_there = await other.post("/v1/calls/outbound", json=dial)

        # The call ends; Twilio tells this server, which frees the slot the
        # other one took.
        done = await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).status_callback_url,
            {**inbound, "CallStatus": "completed", "CallDuration": "3"},
        )
        admitted_there = await other.post("/v1/calls/outbound", json=dial)

    assert "<Connect>" in answered.text
    assert here.text == BUSY
    assert refused_here.status_code == refused_there.status_code == 429
    assert done.status_code == 204
    assert admitted_there.status_code == 201, admitted_there.text
    assert app.carrier.actions_of("dial") == []  # the other server dialled


async def test_webrtc_over_the_limit_is_429_and_leaves_no_call(monkeypatch, redis_env):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, number, _ = await limited_project(app, live_calls=1)
        await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).voice_url,
            ring(app.carrier, number),
        )
        r = await app.api.post(
            "/v1/webrtc/connect",
            json={"sdp": "v=0", "type": "offer", "agent_id": agent_id},
        )
        calls = (await app.api.get("/v1/calls")).json()["data"]

    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"
    assert len(calls) == 1


async def test_whatsapp_over_the_limit_is_rejected_unanswered(monkeypatch, redis_env):
    from pipecat.transports.whatsapp.api import WhatsAppApi

    rejected_ids: list[str] = []

    async def _reject(_self, call_id: str) -> dict:
        rejected_ids.append(call_id)
        return {"success": True}

    async def _never(*_a, **_k):
        raise AssertionError("an over-limit call must not be answered")

    monkeypatch.setattr(WhatsAppApi, "reject_call_to_whatsapp", _reject)
    monkeypatch.setattr(WhatsAppApi, "answer_call_to_whatsapp", _never)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        _, number, _ = await limited_project(app, live_calls=1)
        await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).voice_url,
            ring(app.carrier, number),
        )
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        event = await app.sink.wait_for("call.rejected")

    assert r.status_code == 200
    assert rejected_ids == ["wa-call-1"]
    assert event["payload"]["transport"] == "whatsapp"
    assert event["payload"]["provider_call_id"] == "wa-call-1"


def _whatsapp_connect(number: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BIZ",
                "changes": [
                    {
                        "field": "calls",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": number,
                                "phone_number_id": "PHONE_ID",
                            },
                            "calls": [
                                {
                                    "id": "wa-call-1",
                                    "from": "15559876543",
                                    "to": number.lstrip("+"),
                                    "event": "connect",
                                    "timestamp": "2026-10-09T10:00:00Z",
                                    "direction": "inbound",
                                    "session": {"sdp": "v=0", "sdp_type": "offer"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def test_a_slot_nobody_renews_frees_itself(monkeypatch, redis_env):
    """A crashed server never releases its calls; their leases lapse instead,
    so the project is short of slots for a lease, not locked out for good."""
    import asyncio

    from turncall.config.settings import RedisSettings
    from turncall.storage.redis import close_redis, init_redis

    monkeypatch.setattr(limits, "LIVE_CALL_LEASE_MS", 300)
    await init_redis(RedisSettings())
    try:
        project = uuid.uuid4()
        crashed, waiting = uuid.uuid4(), uuid.uuid4()
        assert await limits.claim_live_call(project, crashed, 1)
        assert not await limits.claim_live_call(project, waiting, 1)
        await asyncio.sleep(0.4)
        assert await limits.claim_live_call(project, waiting, 1)
        await limits.release_live_call(project, waiting)
    finally:
        await close_redis()


async def test_may_dial_asks_without_taking(monkeypatch, redis_env):
    """A campaign worker asks before it dials (#229); asking takes nothing."""
    N = "+15550000001"
    from unittest.mock import AsyncMock

    from turncall.config.settings import RedisSettings
    from turncall.storage.redis import close_redis, init_redis

    monkeypatch.setattr(
        limits,
        "limits_for_project",
        AsyncMock(return_value=limits.ProjectLimits(live_calls=1)),
    )
    await init_redis(RedisSettings())
    try:
        project, call = uuid.uuid4(), uuid.uuid4()
        assert (await limits.may_dial(None, project_id=project, from_number=N)).allowed
        assert (await limits.may_dial(None, project_id=project, from_number=N)).allowed
        assert await limits.claim_live_call(project, call, 1)
        refused = await limits.may_dial(None, project_id=project, from_number=N)
        assert (refused.allowed, refused.limit) == (False, "live_calls")
        await limits.release_live_call(project, call)
    finally:
        await close_redis()
