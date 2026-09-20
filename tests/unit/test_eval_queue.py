"""The eval run queue — a Redis list, and the idle path that is not an error.

The worker spends most of its life blocked on an empty queue. Whether that
reads as "nothing to do" or as an exception decides whether its log is worth
looking at when something actually breaks.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from turncall.evals.queue import QUEUE_KEY, dequeue, enqueue

pytestmark = pytest.mark.unit


async def test_enqueue_pushes_the_run_id_as_text() -> None:
    redis = AsyncMock()
    run_id = uuid4()
    await enqueue(redis, run_id)
    redis.lpush.assert_awaited_once_with(QUEUE_KEY, str(run_id))


async def test_dequeue_returns_the_popped_id() -> None:
    run_id = uuid4()
    redis = AsyncMock()
    redis.brpop.return_value = (QUEUE_KEY, str(run_id))
    assert await dequeue(redis) == run_id


async def test_dequeue_decodes_bytes_too() -> None:
    """decode_responses is on in production, but a pool configured without it
    must not crash the worker."""
    run_id = uuid4()
    redis = AsyncMock()
    redis.brpop.return_value = (b"k", str(run_id).encode())
    assert await dequeue(redis) == run_id


async def test_an_empty_queue_is_not_an_error() -> None:
    redis = AsyncMock()
    redis.brpop.return_value = None
    assert await dequeue(redis) is None


async def test_a_client_side_read_timeout_is_also_just_idle() -> None:
    """redis-py raises its own TimeoutError when the read deadline fires before
    BRPOP returns nil. Letting it escape made an idle worker log a traceback
    every few seconds — which is how a log stops being read."""
    redis = AsyncMock()
    redis.brpop.side_effect = RedisTimeoutError("Timeout reading from localhost:6379")
    assert await dequeue(redis) is None


async def test_a_real_redis_failure_still_propagates() -> None:
    """Idle is not an error; a broken connection is."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis = AsyncMock()
    redis.brpop.side_effect = RedisConnectionError("connection refused")
    with pytest.raises(RedisConnectionError):
        await dequeue(redis)
