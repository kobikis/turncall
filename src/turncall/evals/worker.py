"""`turncall-eval-worker`: the process that actually runs evals.

Never the API process. ADR-0004 is the reason: Twilio paces audio in hard
realtime, so event-loop jitter on a live call becomes dead air for the person
on the phone. An eval run does full STT+LLM+TTS at maximum speed and you want
several at once, which is precisely the load that produces that jitter.

Same container image, new entrypoint, its own concurrency cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from loguru import logger

from turncall.config import get_settings
from turncall.evals import queue as eval_queue


async def _janitor(session_factory: Any, settings: Any, stop: asyncio.Event) -> None:
    """Sweep runs a crashed worker left claimed forever.

    Nothing else would ever move them, and a row stuck at `running` reads to an
    operator as work still in flight.
    """
    from turncall.storage.repositories import eval_repo

    interval = settings.evals.janitor_interval_seconds
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            return
        try:
            async with session_factory() as session:
                swept = await eval_repo.reclaim_stalled_runs(
                    session, max_age_seconds=settings.evals.max_run_duration_seconds
                )
                await session.commit()
            if swept:
                logger.warning("eval_runs_reclaimed", count=swept)
        except Exception:
            logger.exception("eval_janitor_error")


async def _consume(
    session_factory: Any, settings: Any, stop: asyncio.Event, slots: asyncio.Semaphore
) -> None:
    """Pull run ids off the queue and execute them, up to the concurrency cap."""
    from turncall.evals.runner import execute_run
    from turncall.storage.redis import get_redis

    running: set[asyncio.Task] = set()
    redis = get_redis()
    while not stop.is_set():
        # Take the slot before popping: a run popped with nowhere to execute it
        # would have to be pushed back, and a crash in that window loses it.
        await slots.acquire()
        if stop.is_set():
            slots.release()
            break
        try:
            run_id = await eval_queue.dequeue(redis, timeout=5)
        except Exception:
            logger.exception("eval_dequeue_error")
            slots.release()
            await asyncio.sleep(1)
            continue
        if run_id is None:
            slots.release()
            continue

        async def _run(run_id: Any = run_id) -> None:
            try:
                await execute_run(
                    run_id, session_factory=session_factory, settings=settings
                )
            finally:
                slots.release()

        task = asyncio.create_task(_run(), name=f"eval-run-{run_id}")
        running.add(task)
        task.add_done_callback(running.discard)

    if running:
        logger.info("eval_worker_draining", in_flight=len(running))
        await asyncio.gather(*running, return_exceptions=True)


async def run_worker() -> None:
    """Start the worker and serve until signalled."""
    from turncall.storage.database import (
        close_database,
        get_session_factory,
        init_database,
    )
    from turncall.storage.redis import close_redis, init_redis

    settings = get_settings()
    await init_database(settings.database)
    await init_redis(settings.redis)
    session_factory = get_session_factory()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    slots = asyncio.Semaphore(settings.evals.max_concurrent_runs)
    logger.info(
        "eval_worker_started",
        concurrency=settings.evals.max_concurrent_runs,
        queue=eval_queue.QUEUE_KEY,
    )

    janitor = asyncio.create_task(_janitor(session_factory, settings, stop))
    try:
        await _consume(session_factory, settings, stop, slots)
    finally:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        await close_redis()
        await close_database()
        logger.info("eval_worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
