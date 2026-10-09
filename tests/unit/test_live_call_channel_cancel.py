"""The call's own Redis client is closed however `serve` is left (#215).

`serve` opens a dedicated client per call. Its subscribe step cleaned up only
on `Exception`, and `CancelledError` is not one: a call cancelled while
subscribing — a forced shutdown after a second SIGTERM, a caller gone in the
first second — left that client and its connection open.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from turncall.services import live_call_channel

pytestmark = pytest.mark.unit


def _subscriber(subscribe: AsyncMock) -> MagicMock:
    pubsub = MagicMock()
    pubsub.subscribe = subscribe
    pubsub.aclose = AsyncMock()
    client = MagicMock()
    client.pubsub = MagicMock(return_value=pubsub)
    client.aclose = AsyncMock()
    return client


async def test_cancelled_while_subscribing_closes_the_client():
    started = asyncio.Event()

    async def never_confirms(_name: str) -> None:
        started.set()
        await asyncio.Event().wait()

    subscriber = _subscriber(AsyncMock(side_effect=never_confirms))

    async def run() -> None:
        async with live_call_channel.serve(MagicMock(), subscriber, "c1", {}):
            pytest.fail("the channel never came up")

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    subscriber.pubsub.return_value.aclose.assert_awaited_once()
    subscriber.aclose.assert_awaited_once()


async def test_a_failed_subscribe_still_yields_false_and_closes():
    subscriber = _subscriber(AsyncMock(side_effect=ConnectionError("no redis")))
    async with live_call_channel.serve(MagicMock(), subscriber, "c1", {}) as up:
        assert up is False
    subscriber.aclose.assert_awaited_once()
