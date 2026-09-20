"""The eval run queue: a Redis list, and nothing more.

`LPUSH` when a run is created, `BRPOP` in the worker. No Celery — the job is a
uuid, the worker is one process, and a crashed worker is handled by the janitor
sweeping stalled rows rather than by broker-side acknowledgement.
"""

from __future__ import annotations

from uuid import UUID

QUEUE_KEY = "turncall:eval:runs"


async def enqueue(redis: object, run_id: UUID) -> None:
    """Queue a run for the worker."""
    await redis.lpush(QUEUE_KEY, str(run_id))  # type: ignore[attr-defined]


async def dequeue(redis: object, *, timeout: int = 5) -> UUID | None:
    """Block for the next queued run, or return None when the wait expires.

    The timeout is what lets the worker notice a shutdown signal between jobs
    rather than blocking on Redis forever.

    An idle wait ends one of two ways and both mean "no work": BRPOP returns
    nil, or redis-py raises its own `TimeoutError` because the client's read
    deadline fired first. Letting the second escape made the worker log a full
    traceback every few seconds while doing nothing wrong — noise that trains
    an operator to ignore the log this worker's failures are reported in.
    """
    from redis.exceptions import TimeoutError as RedisTimeoutError

    try:
        popped = await redis.brpop([QUEUE_KEY], timeout=timeout)  # type: ignore[attr-defined]
    except RedisTimeoutError:
        return None
    if popped is None:
        return None
    _key, value = popped
    return UUID(value if isinstance(value, str) else value.decode())
