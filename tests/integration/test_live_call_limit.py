"""A project's live-call limit, through the running app (#224).

Driven the way a carrier and an integrator see it: an inbound call over the
limit is refused busy at the carrier and announced as `call.rejected`, never
answered; an outbound one is a 429 and the carrier never hears of it. The
count lives in Redis, so a second server process sees the same one — and every
way a call ends gives its slot back, or a project would be locked out for good.
"""

import uuid

import pytest

from tests.harness import place_inbound_call, post_signed, run_app, telnyx
from tests.harness.second_instance import second_instance
from tests.integration.limits_support import (
    AGENT,
    SCRIPT,
    dial_body,
    limited_project,
    ring,
)
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


async def test_over_the_limit_telnyx_inbound_is_rejected_busy(monkeypatch, redis_env):
    """The limit is checked where both carriers' inbound calls are opened
    (#217): Telnyx refuses with a `reject` command, cause busy, never `answer`."""

    def _call(call_control_id: str, to_number: str, kind: str) -> dict:
        return telnyx.event(
            kind,
            {
                "call_control_id": call_control_id,
                "from": "+15550009999",
                "to": to_number,
                "direction": "incoming",
                "hangup_cause": "normal_clearing",
            },
        )

    first, over, later = (f"v3:{uuid.uuid4().hex}" for _ in range(3))
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await telnyx.bind_number(
            app.api, app.carrier, await app.create_agent(AGENT)
        )
        r = await app.api.put(
            f"/v1/projects/{app.project_id}/limits", json={"live_calls": 1}
        )
        assert r.status_code == 200, r.text

        for cc_id, kind in ((first, "call.initiated"), (over, "call.initiated")):
            sent = await telnyx.post_signed(
                app.api, app.carrier, _call(cc_id, number, kind)
            )
            assert sent.status_code == 200, sent.text
        rejected = await app.sink.wait_for("call.rejected")
        calls = (await app.api.get("/v1/calls")).json()["data"]

        # The first call ends before its stream opened: Telnyx's `call.hangup`
        # gives its slot back, and the next call is answered.
        for cc_id, kind in ((first, "call.hangup"), (later, "call.initiated")):
            await telnyx.post_signed(app.api, app.carrier, _call(cc_id, number, kind))

    answered = [a.params["call_sid"] for a in app.carrier.actions_of("answer")]
    refused = app.carrier.actions_of("reject")
    assert answered == [first, later]
    assert [(a.params["call_sid"], a.params["cause"]) for a in refused] == [
        (over, "USER_BUSY")
    ]
    assert rejected["call_id"] is None
    assert rejected["payload"]["transport"] == "telnyx"
    assert rejected["payload"]["provider_call_id"] == over
    assert [c["provider_call_sid"] for c in calls] == [first]


async def test_over_the_limit_asterisk_call_goes_back_to_the_dialplan(
    monkeypatch, redis_env
):
    """The ARI edge admits through the same `open_inbound_call` (#209): over
    the limit the caller's channel is continued to the dialplan unanswered, as
    any refused call is, and `call.rejected` names the carrier. A call whose
    caller leaves before its media stream came gives its slot back — no
    pipeline ever held it."""
    import asyncio

    from turncall.storage.redis import get_redis

    async def _slot_freed(project_id: str, call_id: str) -> None:
        key = limits._live_calls_key(uuid.UUID(project_id))
        for _ in range(100):
            if await get_redis().zscore(key, call_id) is None:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the abandoned call's slot was never freed")

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_asterisk_number(await app.create_agent(AGENT))
        r = await app.api.put(
            f"/v1/projects/{app.project_id}/limits", json={"live_calls": 1}
        )
        assert r.status_code == 200, r.text
        pbx = app.asterisk

        # Admitted, answered, its external media asked for — which never dials.
        pbx.media_dials = False
        abandoned = await pbx.ring("+15550001111", number, until_bridged=False)
        for _ in range(100):
            if abandoned.media_id:
                break
            await asyncio.sleep(0.05)
        assert abandoned.call_id, "the first call was not admitted"

        over = await pbx.ring("+15550002222", number, until_bridged=False)
        sent_back = await app.carrier.wait_for("continue")
        rejected = await app.sink.wait_for("call.rejected")

        # The first caller gives up before any media: the edge frees the slot.
        await pbx.leave_stasis(abandoned)
        await _slot_freed(app.project_id, abandoned.call_id)
        pbx.media_dials = True
        later = await pbx.ring("+15550003333", number)
        await later.hang_up()
        calls = (await app.api.get("/v1/calls")).json()["data"]

    assert sent_back.params["call_sid"] == over.call_sid
    assert [a.params["call_sid"] for a in app.carrier.actions_of("continue")] == [
        over.call_sid
    ]
    assert not over.call_id  # never admitted: no TURNCALL_CALL_ID, no answer
    answered = [p for m, p, _ in pbx.requests if m == "POST" and p.endswith("/answer")]
    assert f"channels/{over.call_sid}/answer" not in answered
    assert rejected["call_id"] is None
    assert rejected["payload"]["reason"] == "live_call_limit"
    assert rejected["payload"]["transport"] == "asterisk"
    assert rejected["payload"]["provider_call_id"] == over.call_sid
    status = {c["id"]: c["status"] for c in calls}
    assert status[abandoned.call_id] == "failed"
    assert later.call_id in status
    assert len(status) == 2  # the refused call wrote no row


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


async def test_reserve_dial_takes_both_or_nothing(monkeypatch, redis_env):
    """A campaign reserves its dial's slots before dialling (#233): the dial
    under that id is then admitted without counting twice, another id is
    refused, and a refusal or a release takes nothing."""
    N = f"+1555{uuid.uuid4().int % 10**7:07d}"
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
        project, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        async def reserve(call: uuid.UUID, floor: int = 0) -> limits.DialCheck:
            return await limits.reserve_dial(
                None,
                project_id=project,
                call_id=call,
                from_number=N,
                default_calls_per_second=floor,
            )

        assert (await reserve(first)).allowed
        # The dial itself, under the reserved id: admitted, not counted again.
        await limits.admit_outbound_call(
            None, project_id=project, call_id=first, from_number=N
        )
        refused = await reserve(second)
        assert (refused.allowed, refused.limit, refused.max) == (
            False,
            "live_calls",
            1,
        )
        await limits.release_dial(project, N, first)
        # The project sets no rate: the floor paces the number instead.
        assert (await reserve(second, floor=1)).allowed
        await limits.release_live_call(project, second)
        paced = await reserve(first, floor=1)
        assert (paced.allowed, paced.limit) == (False, "outbound_calls_per_second")
        assert 0 < paced.retry_after_ms <= 1000
        # ...and nothing was taken by the refusal.
        assert await limits.claim_live_call(project, first, 1)
        await limits.release_dial(project, N, first)
    finally:
        await close_redis()
