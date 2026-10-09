"""The campaign worker's wake-up: a Redis list of campaign ids.

Unlike the eval queue, the list is not where the work lives. A campaign runs
for hours and a worker restarts in the middle of it, so the database is what
the worker reads each pass; a pushed id only cuts the wait before a new
campaign's first dial. Losing one costs a poll interval, never a campaign.
"""

from __future__ import annotations

from uuid import UUID

QUEUE_KEY = "turncall:campaigns:created"


async def nudge(redis: object, campaign_id: UUID) -> None:
    """Tell a waiting worker there is a new campaign."""
    await redis.lpush(QUEUE_KEY, str(campaign_id))  # type: ignore[attr-defined]


async def wait(redis: object, *, timeout: int) -> None:
    """Return when a campaign is created, or after `timeout` seconds."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    try:
        await redis.brpop([QUEUE_KEY], timeout=timeout)  # type: ignore[attr-defined]
    except RedisTimeoutError:
        # The client's read deadline fired first: an idle wait, not a fault.
        return
