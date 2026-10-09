"""Live calls on this process, and letting them finish on SIGTERM (#199).

A deploy or a scale-down sends SIGTERM. Uvicorn's own answer is to close the
listening socket and every open connection at once — a Twilio media stream
gets close code 1012 mid-sentence — so a replica that is stopped cuts off
every call it carries. This module intercepts that SIGTERM instead:

1. **Readiness fails at once** (`is_draining()`; `/ready` answers 503), so a
   load balancer routes new calls elsewhere. Liveness stays green — the
   process is healthy, just leaving.
2. **The process keeps serving** while its live calls end on their own, then
   while their post-call work (analysis, the `call.ended` webhook) goes out.
3. Only then is the SIGTERM handed back to whoever had it (uvicorn), which
   shuts down as usual with nothing left to cut off.

The wait is bounded: every call carries its own `max_call_duration_seconds`,
and the drain gives up at the latest moment any live call's duration guard
would have ended it (plus `GUARD_SLACK_SECONDS`), then allows post-call work
`POST_CALL_GRACE_SECONDS`. A second SIGTERM hands over at once.

Counted per process: a call is live from the moment its transport entry point
knows its agent until its session has finalized. Nothing here imports pipecat.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any
from uuid import UUID

from loguru import logger

# AgentConfig.max_call_duration_seconds' own default, for a call with none.
DEFAULT_MAX_CALL_DURATION_SECONDS = 3600
# Between a call being counted and its duration guard starting there is the
# pipeline build (MCP discovery alone may take MCP_CONNECT_TIMEOUT_SECONDS),
# and after the guard fires there is finalizing.
GUARD_SLACK_SECONDS = 30
# Post-call analysis plus webhook delivery's own retries (1+2+4+8+16s).
POST_CALL_GRACE_SECONDS = 60
POLL_SECONDS = 0.2

# call_id -> monotonic time by which its duration guard will have ended it.
_live: dict[UUID, float] = {}
_draining = False
_drain_task: asyncio.Task | None = None
_handler: Callable[[int, FrameType | None], None] | None = None
_previous: Any = signal.SIG_DFL  # SIGTERM's owner before us


@contextmanager
def live_call(call_id: UUID, max_duration_seconds: int | None) -> Iterator[None]:
    """Count one call as live on this process for the duration of the block."""
    budget = max_duration_seconds or DEFAULT_MAX_CALL_DURATION_SECONDS
    _live[call_id] = time.monotonic() + budget + GUARD_SLACK_SECONDS
    try:
        yield
    finally:
        _live.pop(call_id, None)


def active_calls() -> int:
    """Live calls on this process — the `turncall_active_calls` gauge."""
    return len(_live)


def is_draining() -> bool:
    """True from the first SIGTERM on: readiness must fail."""
    return _draining


def _post_call_work() -> Collection[asyncio.Task]:
    """What still has to leave the process for a finished call to be finished:
    post-call processing and the webhook deliveries it (and the call) queued."""
    from turncall.events.dispatcher import pending_deliveries
    from turncall.services.call_analysis_trigger import pending_post_call_tasks

    return [*pending_post_call_tasks(), *pending_deliveries()]


async def drain(
    pending_work: Callable[[], Collection[asyncio.Task]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for live calls to end, then for their post-call work; bounded."""
    pending_work = pending_work or _post_call_work
    logger.info("drain_started", active_calls=active_calls())
    while _live and clock() < max(_live.values()):
        await asyncio.sleep(POLL_SECONDS)
    if _live:
        logger.warning("drain_deadline_reached", active_calls=active_calls())

    grace_ends = clock() + POST_CALL_GRACE_SECONDS
    while pending_work() and clock() < grace_ends:
        await asyncio.sleep(POLL_SECONDS)
    if pending_work():
        logger.warning("drain_post_call_work_abandoned", tasks=len(pending_work()))
    logger.info("drain_finished")


def install_sigterm_drain() -> None:
    """Take SIGTERM over for the drain; hand it back to its previous owner after.

    Called from the app's lifespan, which uvicorn runs *after* installing its
    own handler — so the one replaced here is uvicorn's, and handing the signal
    back is re-raising it with uvicorn's handler restored. Main thread only
    (signals cannot be set elsewhere): an app run under a test client's thread
    keeps whatever handler it had.
    """
    global _draining, _handler, _previous
    if threading.current_thread() is not threading.main_thread():
        logger.debug("sigterm_drain_not_installed: not on the main thread")
        return
    loop = asyncio.get_running_loop()

    def start_drain() -> None:
        global _drain_task
        _drain_task = loop.create_task(drain())
        _drain_task.add_done_callback(lambda _task: _hand_back())

    def on_sigterm(_sig: int, _frame: FrameType | None) -> None:
        global _draining
        if _draining:  # a second SIGTERM: stop waiting
            loop.call_soon_threadsafe(_hand_back)
            return
        _draining = True  # readiness fails from this instant
        loop.call_soon_threadsafe(start_drain)

    _draining = False
    _previous = signal.getsignal(signal.SIGTERM)
    _handler = on_sigterm
    signal.signal(signal.SIGTERM, on_sigterm)


def _hand_back() -> None:
    """Restore SIGTERM's previous owner and deliver the signal to it — once."""
    if uninstall_sigterm_drain():
        logger.info("drain_handing_sigterm_back")
        signal.raise_signal(signal.SIGTERM)


def uninstall_sigterm_drain() -> bool:
    """Give SIGTERM back to its previous owner. True if ours was installed."""
    global _handler
    ours = _handler is not None and signal.getsignal(signal.SIGTERM) is _handler
    if ours:
        signal.signal(signal.SIGTERM, _previous)
    _handler = None
    return ours
