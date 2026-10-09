"""A live call's control channel: how an API request reaches the call (#215).

The call runs in whichever task — on whichever replica — holds its media, and
the API request that wants to change it lands anywhere. Before this, the API
wrote an event and answered "Context injected" while the call's model never
saw a word (tracker #198).

The protocol, one Redis pub/sub channel per call (`call:{id}:control`):

- The task running the call subscribes for the call's lifetime (`serve`).
- The API publishes a `ControlRequest` (`request`). `PUBLISH` answers with how
  many subscribers received it: zero is `CallNotLive` — the call is not running
  anywhere — never a success.
- The call applies it in its own task and pushes an `Ack` onto a list named
  for the request. The API waits up to `ACK_TIMEOUT_SECONDS` for it: none is
  `CallDidNotAcknowledge`, so a call that received a request and did nothing
  can never read as one that applied it.

A request names its `kind`; the call answers through the handler registered
for that kind, so another request (#216's handoff) is one more handler, not a
second channel. Handlers run one at a time, in arrival order.

No pipecat here: what a handler does to the pipeline is the orchestrator's.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger
from redis.asyncio import Redis

# How long the API waits for the call to answer. Applying a request is a frame
# queued in the call's own process, so a healthy call answers in milliseconds;
# two seconds is an unhealthy one, and the API says so rather than hang.
ACK_TIMEOUT_SECONDS = 2.0
# An answer nobody collected (the API gave up first) expires on its own.
_ACK_TTL_SECONDS = 30
_SUBSCRIBE_TIMEOUT_SECONDS = 2.0

# The request kinds. #216 adds the handoff beside it.
MESSAGE = "message"  # add a message to the model's context

# A handler takes the request's body and returns what it applied, which the
# API hands back to its caller. It raises `ControlRefused` to decline.
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class CallNotLive(Exception):
    """No task is running the call: nothing received the request."""


class CallDidNotAcknowledge(Exception):
    """The call received the request and did not answer in time."""


class ControlRefused(Exception):
    """Raised by a handler: the call cannot apply this request, and why."""


@dataclass(frozen=True)
class ControlRequest:
    kind: str
    body: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> ControlRequest:
        data = json.loads(raw)
        return cls(kind=str(data["kind"]), body=dict(data["body"]), id=str(data["id"]))


@dataclass(frozen=True)
class Ack:
    """The call's answer: what it applied, or why it would not."""

    ok: bool
    applied: dict[str, Any] | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Ack:
        data = json.loads(raw)
        return cls(
            ok=bool(data["ok"]), applied=data.get("applied"), error=data.get("error")
        )


def channel(call_id: uuid.UUID | str) -> str:
    return f"call:{call_id}:control"


def _ack_key(request_id: str) -> str:
    return f"call:control:ack:{request_id}"


async def request(
    redis: Redis,
    call_id: uuid.UUID | str,
    kind: str,
    body: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Ack:
    """Ask the live call to apply `kind`, and return its answer.

    Raises `CallNotLive` when nothing is running the call and
    `CallDidNotAcknowledge` when it does not answer within the timeout.
    """
    req = ControlRequest(kind=kind, body=body)
    receivers = await redis.publish(channel(call_id), req.to_json())
    if receivers == 0:
        raise CallNotLive(str(call_id))
    reply = await redis.blpop(
        [_ack_key(req.id)], timeout=timeout or ACK_TIMEOUT_SECONDS
    )
    if reply is None:
        raise CallDidNotAcknowledge(str(call_id))
    return Ack.from_json(reply[1])


@asynccontextmanager
async def serve(
    redis: Redis,
    subscriber: Redis,
    call_id: uuid.UUID | str,
    handlers: Mapping[str, Handler],
) -> AsyncIterator[bool]:
    """Answer the call's control requests for as long as the block runs.

    `subscriber` is a client of the call's own, closed here: a subscription
    holds its connection for the whole call, so one from the shared pool
    (`redis`, used for the answers) would let `REDIS_MAX_CONNECTIONS`
    concurrent calls starve every other Redis user in the process.

    Yields whether the channel is up. It never fails the call: a Redis that
    cannot be reached costs the call its control channel — every request then
    gets a 409 — and is logged, while the caller keeps talking.
    """
    pubsub = subscriber.pubsub()
    try:
        # Bounded: the call's audio waits on this, and a Redis that accepts
        # nothing would otherwise hold it for the OS's connect timeout.
        await asyncio.wait_for(
            _subscribed(pubsub, channel(call_id)), _SUBSCRIBE_TIMEOUT_SECONDS
        )
    except Exception as exc:
        logger.warning(
            "control_channel_unavailable", call_id=str(call_id), error=repr(exc)
        )
        await _close(pubsub, subscriber, call_id)
        yield False
        return

    listener = asyncio.create_task(_listen(redis, pubsub, call_id, handlers))
    try:
        yield True
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await _close(pubsub, subscriber, call_id)


async def _subscribed(pubsub: Any, name: str) -> None:
    """Subscribe, and return once Redis has confirmed it.

    `subscribe()` only writes the command; until the confirmation is read, a
    request published meanwhile could find no receiver and be answered 409.
    """
    await pubsub.subscribe(name)
    while True:
        message = await pubsub.get_message(timeout=_SUBSCRIBE_TIMEOUT_SECONDS)
        if message is not None and message["type"] == "subscribe":
            return


async def _close(pubsub: Any, client: Redis, call_id: uuid.UUID | str) -> None:
    try:
        await pubsub.aclose()
        await client.aclose(close_connection_pool=True)
    except Exception:
        logger.exception("control_channel_close_error", call_id=str(call_id))


async def _listen(
    redis: Redis,
    pubsub: Any,
    call_id: uuid.UUID | str,
    handlers: Mapping[str, Handler],
) -> None:
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await _answer(redis, message["data"], call_id, handlers)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The call goes on; requests to it now get a 409.
        logger.exception("control_channel_lost", call_id=str(call_id))


async def _answer(
    redis: Redis,
    raw: str | bytes,
    call_id: uuid.UUID | str,
    handlers: Mapping[str, Handler],
) -> None:
    try:
        req = ControlRequest.from_json(raw)
    except (ValueError, KeyError, TypeError):
        logger.warning("control_request_unreadable", call_id=str(call_id))
        return
    ack = await _apply(req, call_id, handlers)
    key = _ack_key(req.id)
    try:
        await redis.rpush(key, ack.to_json())
        await redis.expire(key, _ACK_TTL_SECONDS)
    except Exception:
        logger.exception("control_ack_failed", call_id=str(call_id), kind=req.kind)


async def _apply(
    req: ControlRequest, call_id: uuid.UUID | str, handlers: Mapping[str, Handler]
) -> Ack:
    handler = handlers.get(req.kind)
    if handler is None:
        return Ack(ok=False, error=f"This call does not handle '{req.kind}' requests")
    try:
        applied = await handler(req.body)
    except ControlRefused as exc:
        return Ack(ok=False, error=str(exc))
    except Exception:
        logger.exception("control_request_failed", call_id=str(call_id), kind=req.kind)
        return Ack(ok=False, error=f"The call failed to apply the '{req.kind}' request")
    logger.info("control_request_applied", call_id=str(call_id), kind=req.kind)
    return Ack(ok=True, applied=applied)
