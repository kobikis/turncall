"""The control channel's protocol, against a real Redis (#215).

The endpoint's three answers rest on it: no receiver is `CallNotLive`, a
receiver that never answers is `CallDidNotAcknowledge`, and only an answer
from the call itself is a result. Skips without a reachable Redis.
"""

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from turncall.services import live_call_channel as lcc

pytestmark = pytest.mark.integration

URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _client() -> Redis:
    return Redis.from_url(URL, decode_responses=True)


@pytest.fixture
async def redis():
    client = _client()
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable ({type(exc).__name__})")
    yield client
    await client.aclose()


async def _echo(body: dict) -> dict:
    return {"echo": body}


async def _refuse(_body: dict) -> dict:
    raise lcc.ControlRefused("not on this call")


async def _crash(_body: dict) -> dict:
    raise RuntimeError("boom")


async def test_nothing_running_the_call_is_not_live(redis) -> None:
    with pytest.raises(lcc.CallNotLive):
        await lcc.request(redis, uuid.uuid4(), "message", {})


async def test_a_receiver_that_never_answers_is_not_a_success(redis) -> None:
    """The bug this replaces: a request delivered is not a request applied."""
    call_id = uuid.uuid4()
    deaf = _client().pubsub()
    await deaf.subscribe(lcc.channel(call_id))
    await deaf.get_message(timeout=1)  # the subscription is confirmed
    try:
        with pytest.raises(lcc.CallDidNotAcknowledge):
            await lcc.request(redis, call_id, "message", {}, timeout=0.3)
    finally:
        await deaf.aclose()


async def test_the_calls_answer_is_the_result(redis) -> None:
    call_id = uuid.uuid4()
    handlers = {"message": _echo, "refuse": _refuse, "crash": _crash}
    async with lcc.serve(redis, _client(), call_id, handlers) as up:
        assert up
        applied = await lcc.request(redis, call_id, "message", {"a": 1})
        refused = await lcc.request(redis, call_id, "refuse", {})
        crashed = await lcc.request(redis, call_id, "crash", {})
        unknown = await lcc.request(redis, call_id, "handoff", {})

    assert applied == lcc.Ack(ok=True, applied={"echo": {"a": 1}})
    assert refused == lcc.Ack(ok=False, error="not on this call")
    assert not crashed.ok and "failed to apply" in crashed.error
    assert not unknown.ok and "'handoff'" in unknown.error

    # Torn down with the block: the call is no longer reachable.
    with pytest.raises(lcc.CallNotLive):
        await lcc.request(redis, call_id, "message", {})


async def test_requests_are_applied_one_at_a_time(redis) -> None:
    """Never two changes to one call's pipeline at once (#216's handoff relies
    on it), and every concurrent request still gets its own answer."""
    call_id = uuid.uuid4()
    active, overlap, seen = 0, 0, []

    async def slow(body: dict) -> dict:
        nonlocal active, overlap
        active += 1
        overlap = max(overlap, active)
        await asyncio.sleep(0.05)
        active -= 1
        seen.append(body["n"])
        return {"n": body["n"]}

    async with lcc.serve(redis, _client(), call_id, {"message": slow}):
        acks = await asyncio.gather(
            *(lcc.request(redis, call_id, "message", {"n": n}) for n in range(3))
        )
    assert overlap == 1
    assert [a.applied for a in acks] == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert sorted(seen) == [0, 1, 2]


async def test_an_unreachable_redis_never_fails_the_call(redis) -> None:
    nowhere = Redis.from_url("redis://127.0.0.1:1/0")
    async with lcc.serve(redis, nowhere, uuid.uuid4(), {}) as up:
        assert up is False


async def test_a_call_is_running_while_its_channel_is_served(redis) -> None:
    """What the post-call sweep reads to leave a live pipeline alone (#356)."""
    call_id = uuid.uuid4()
    assert not await lcc.is_running(redis, call_id)
    async with lcc.serve(redis, _client(), call_id, {"message": _echo}):
        assert await lcc.is_running(redis, call_id)
    assert not await lcc.is_running(redis, call_id)
