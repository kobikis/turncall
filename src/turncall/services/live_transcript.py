"""A call's conversation as it was said, held in this process's memory (#313).

Redaction (ADR-0024) rewrites the transcript before it is stored, and must
never reach what the call itself uses. Two things read the conversation on the
call's behalf after the words were spoken: the warm-transfer briefing (the
operator needs the caller's name and number) and post-call analysis with its
takeaways (they extract that number). Both read it here, raw; only their
outputs are redacted on the way out.

The transcript taps (`orchestrator/observability.py`) add each utterance the
moment they log it, so the lines are in the order they were said. The process
running the call holds them until `KEEP_AFTER_CALL_SECONDS` after its pipeline
stops — long enough for a warm transfer's operator to answer and for post-call
processing to run — then forgets them. Nothing here is stored or sent.

**Another replica** (Twilio's whisper or `/status` callback, the end API) has
none of it, and the conversation must not be copied into Redis or Postgres to
reach it: keeping it in the call's process is the point. So the holder is
asked to do the work instead (`ask_holder`): while it holds a call's
conversation, it is subscribed to `call:{id}:conversation` on one Redis
pub/sub connection for the whole process (`serve_holder`), and answers a
`POST_CALL` request by running post-call processing itself, and a `BRIEFING`
request with the briefing text (spoken to the operator anyway). The request
names the call and nothing else. Only a redacting project's readers ask: for
the rest the stored transcript is the conversation.

When no process holds it (a restart, or the minutes are up), readers fall back
to the stored transcript (redacted with redaction on) and log it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

# The transfer intent lives this long too (services/transfer.py), so a
# briefing resolved while the intent is still parked finds the conversation.
KEEP_AFTER_CALL_SECONDS = 300.0


@dataclass(frozen=True)
class Line:
    """One utterance. `role` is "assistant" for the agent, anything else (the
    caller's transport id, "customer") for the caller — as on the event."""

    role: str
    text: str

    @property
    def is_agent(self) -> bool:
        return self.role == "assistant"


_LINES: dict[UUID, list[Line]] = {}
_FORGET: dict[UUID, asyncio.TimerHandle] = {}


def said(call_id: UUID, role: str, text: str) -> None:
    """Add an utterance to the call's conversation."""
    if call_id not in _LINES:
        _HOLDER.hold(call_id)
    _LINES.setdefault(call_id, []).append(Line(role, text))


def lines(call_id: UUID) -> list[Line] | None:
    """The call's conversation, or None when this process does not hold it."""
    held = _LINES.get(call_id)
    return list(held) if held is not None else None


def release(call_id: UUID, *, after: float = KEEP_AFTER_CALL_SECONDS) -> None:
    """The call's pipeline has stopped: forget its conversation `after`
    seconds from now."""
    if (pending := _FORGET.pop(call_id, None)) is not None:
        pending.cancel()
    if after <= 0:
        _forget(call_id)
        return
    _FORGET[call_id] = asyncio.get_running_loop().call_later(after, _forget, call_id)


def _forget(call_id: UUID) -> None:
    _FORGET.pop(call_id, None)
    if _LINES.pop(call_id, None) is not None:
        _HOLDER.drop(call_id)


# --- Asking the holder, from another replica --------------------------------

POST_CALL = "post_call"  # run post-call processing (call_analysis_trigger)
BRIEFING = "briefing"  # answer the warm-transfer briefing text (transfer)
# The briefing is an LLM summary on the holder; the operator is waiting on
# the line meanwhile, and Twilio gives the whisper URL 15 s.
_ANSWER_WITHIN = {POST_CALL: 2.0, BRIEFING: 10.0}


def channel(call_id: UUID) -> str:
    return f"call:{call_id}:conversation"


async def ask_holder(call_id: UUID, kind: str) -> dict[str, Any] | None:
    """Ask the process holding the call's conversation to do `kind`; what it
    answered, or None when no process holds it or it did not do it."""
    from turncall.services import live_call_channel
    from turncall.storage.redis import get_redis

    try:
        ack = await live_call_channel.request(
            get_redis(),
            call_id,
            kind,
            {"call_id": str(call_id)},
            timeout=_ANSWER_WITHIN[kind],
            to=channel(call_id),
        )
    except live_call_channel.CallNotLive:
        return None
    except Exception as exc:
        logger.warning(
            "conversation_holder_unanswered",
            call_id=str(call_id),
            kind=kind,
            error=repr(exc),
        )
        return None
    if not ack.ok:
        logger.warning(
            "conversation_holder_refused",
            call_id=str(call_id),
            kind=kind,
            error=ack.error,
        )
        return None
    return ack.applied or {}


async def _post_call(body: dict[str, Any]) -> dict[str, Any]:
    from turncall.services.call_analysis_trigger import post_call_here

    return await post_call_here(body)


async def _briefing(body: dict[str, Any]) -> dict[str, Any]:
    from turncall.services.transfer import briefing_here

    return await briefing_here(body)


_HANDLERS = {POST_CALL: _post_call, BRIEFING: _briefing}


class _Holder:
    """This process's subscription to the conversations it holds: one
    connection, a channel per held call, added and dropped as calls come and
    go. Down (no Redis), it costs only the routing: other replicas find no
    holder and fall back."""

    def __init__(self) -> None:
        self._pubsub: Any = None
        self._client: Any = None
        self._redis: Any = None
        self._listener: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self, redis: Any, subscriber: Any) -> None:
        self._redis = redis
        self._client = subscriber
        self._pubsub = subscriber.pubsub()
        # Subscribed to its own channel from the start, so the listener
        # always has a subscription to wait on.
        await self._pubsub.subscribe(f"conversations:{id(self)}")
        for call_id in list(_LINES):
            await self._pubsub.subscribe(channel(call_id))
        self._listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        pubsub, self._pubsub = self._pubsub, None
        client, self._client = self._client, None
        if pubsub is not None:
            try:
                await pubsub.aclose()
                await client.aclose(close_connection_pool=True)
            except Exception:
                logger.exception("conversation_holder_close_error")

    def hold(self, call_id: UUID) -> None:
        if self._pubsub is not None:
            self._spawn(self._pubsub.subscribe(channel(call_id)), call_id)

    def drop(self, call_id: UUID) -> None:
        if self._pubsub is not None:
            self._spawn(self._pubsub.unsubscribe(channel(call_id)), call_id)

    def _spawn(self, work: Any, call_id: UUID) -> None:
        async def run() -> None:
            try:
                await work
            except Exception:
                logger.exception("conversation_holder_error", call_id=str(call_id))

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _listen(self) -> None:
        from turncall.services import live_call_channel

        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("conversation_holder_lost")
                return
            if not message or message.get("type") != "message":
                continue
            call_id = str(message["channel"]).split(":")[1]
            # Each answered on its own: a briefing waits on an LLM.
            self._spawn(
                live_call_channel.answer(
                    self._redis, message["data"], call_id, _HANDLERS
                ),
                UUID(call_id),
            )


_HOLDER = _Holder()


async def serve_holder() -> None:
    """Start answering for the conversations this process holds (lifespan)."""
    from turncall.storage.redis import dedicated_client, get_redis

    try:
        await _HOLDER.start(get_redis(), dedicated_client())
    except Exception as exc:
        logger.warning("conversation_holder_unavailable", error=repr(exc))
        await _HOLDER.stop()


async def stop_holder() -> None:
    await _HOLDER.stop()
