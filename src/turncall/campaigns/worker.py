"""`turncall-campaign-worker`: the process that dials campaigns (#229).

Never the API process, on the eval worker's pattern (ADR-0018): a campaign is
a loop that runs for hours, and the API's event loop is carrying live calls.
Same image, its own entrypoint. Any number of these may run at once — targets
are claimed with `SKIP LOCKED`, never handed out in memory.

A pass is never cancelled halfway: a stop request is honoured between passes,
so a dial the worker started is recorded before it exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from loguru import logger

from turncall.campaigns import queue as campaign_queue
from turncall.campaigns.dialer import Clock, SessionFactory, run_pass, system_clock
from turncall.config import get_settings


async def serve(
    session_factory: SessionFactory,
    redis: Any,
    stop: asyncio.Event,
    *,
    poll_seconds: int,
    clock: Clock = system_clock,
) -> None:
    """Run passes until `stop` is set, waking early when a campaign is created.

    A pass a rate limit cut short is followed by the next as soon as the limit
    frees (#233), so a campaign keeps the number's pace instead of one poll's.
    """
    while not stop.is_set():
        rate_wait = await run_pass(session_factory, clock=clock)
        if stop.is_set():
            return
        if rate_wait is not None and rate_wait < poll_seconds:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=rate_wait)
            continue
        try:
            await campaign_queue.wait(redis, timeout=poll_seconds)
        except Exception:
            # Redis down: the database still holds the work, so keep polling.
            logger.exception("campaign_wait_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


async def run_worker() -> None:
    """Start the worker and serve until signalled."""
    from turncall.storage.database import (
        close_database,
        get_session_factory,
        init_database,
    )
    from turncall.storage.redis import close_redis, get_redis, init_redis
    from turncall.storage.repositories import credential_repo

    settings = get_settings()
    await init_database(settings.database)
    await init_redis(settings.redis)
    session_factory = get_session_factory()

    # A number's own Twilio token is sealed under the credential key; fail
    # closed at start, as the API does, rather than on the first dial.
    async with session_factory() as session:
        await credential_repo.ensure_readable(session, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    poll = settings.campaigns.poll_seconds
    logger.info("campaign_worker_started", poll_seconds=poll)
    try:
        await serve(session_factory, get_redis(), stop, poll_seconds=poll)
    finally:
        await close_redis()
        await close_database()
        logger.info("campaign_worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
