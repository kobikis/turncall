"""Concurrency and rate limits, counted in Redis so every server shares one
count (#206; CONTEXT.md, "concurrency limit vs rate limit").

One primitive serves both kinds: a sorted set whose members each carry an
expiry, read on Redis's own clock so two servers whose clocks disagree still
agree on the count. A rate limit's member expires one window after it was
admitted, which makes the set a sliding window. A live call's member is a
*lease* that whoever runs the call renews while it lasts.

The lease is the answer to "what frees a slot when the process holding the
call dies": nothing has to. The holder stops renewing and the slot frees itself
within `LIVE_CALL_LEASE_MS`, so a crash costs a project a slot for minutes, never
for good. Every orderly ending frees it at once as well — `hold_live_call` on
every voice transport, the Twilio status callback, a dial that failed — and the
lease covers the endings nobody gets to run code in: a killed process, a caller
who hangs up before the media stream connects.

A limit is a guardrail, so Redis being unreachable never refuses a call or a
message: it is logged and admitted (fails open).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.errors import RateLimitedError
from turncall.config import get_settings
from turncall.domain.enums import CallEventType
from turncall.storage.redis import get_redis
from turncall.storage.repositories import project_repo

# Long enough to cover the stretch nobody renews: an outbound call ringing
# (Twilio gives up after 60 s), or the gap between Twilio's webhook and its
# media stream connecting, possibly to another server.
LIVE_CALL_LEASE_MS = 120_000
LIVE_CALL_RENEW_SECONDS = 30
DIAL_WINDOW_MS = 1_000

# KEYS[1]: the set. ARGV: member, limit (0 = none), ttl ms, peek ("1" = count
# only). Returns 0 when admitted, else the ms until the oldest member expires.
_ADMIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local limit = tonumber(ARGV[2])
if limit > 0 and not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  if redis.call('ZCARD', KEYS[1]) >= limit then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return math.max(1, tonumber(first[2]) - now)
  end
end
if ARGV[4] == '1' then return 0 end
local ttl = tonumber(ARGV[3])
redis.call('ZADD', KEYS[1], now + ttl, ARGV[1])
redis.call('PEXPIRE', KEYS[1], ttl)
return 0
"""


@dataclass(frozen=True)
class ProjectLimits:
    """A project's effective limits; 0 means unlimited."""

    live_calls: int = 0
    outbound_calls_per_second: int = 0


@dataclass(frozen=True)
class DialCheck:
    """Whether a dial would be admitted now, without taking anything (#229).

    `limit` names the one that says no; `retry_after_ms` is when a rate limit
    frees up (0 for the live-call limit, which frees when a call ends).
    """

    allowed: bool
    limit: str | None = None
    retry_after_ms: int = 0


async def limits_for_project(session: AsyncSession, project_id: UUID) -> ProjectLimits:
    """The project's own value where it set one, else the platform default."""
    row = await project_repo.get_project_by_id(session, project_id)
    defaults = get_settings().limits

    def pick(name: str) -> int:
        own = getattr(row, f"limit_{name}", None) if row is not None else None
        return getattr(defaults, name) if own is None else own

    return ProjectLimits(
        live_calls=pick("live_calls"),
        outbound_calls_per_second=pick("outbound_calls_per_second"),
    )


async def _admit(
    key: str, member: str, limit: int, ttl_ms: int, *, peek: bool = False
) -> int:
    """0 when admitted (and counted, unless `peek`), else ms until a slot frees."""
    try:
        script = get_redis().register_script(_ADMIT)
        return int(await script(keys=[key], args=[member, limit, ttl_ms, int(peek)]))
    except Exception as exc:
        logger.warning("limit_unchecked_redis_unavailable key={} error={}", key, exc)
        return 0


def _live_calls_key(project_id: UUID) -> str:
    return f"limits:live_calls:{project_id}"


def _dials_key(from_number: str) -> str:
    # Per number, not per project: a carrier's cap is on the number.
    return f"limits:dials:{from_number}"


async def claim_live_call(project_id: UUID, call_id: UUID, limit: int) -> bool:
    """Take a live-call slot for `call_id`; False when the project is full.

    Every live call is counted, limited or not, so a limit set mid-afternoon
    counts the calls already running.
    """
    return (
        await _admit(
            _live_calls_key(project_id), str(call_id), limit, LIVE_CALL_LEASE_MS
        )
        == 0
    )


async def release_live_call(project_id: UUID, call_id: UUID) -> None:
    """Free `call_id`'s slot. Idempotent; never raises."""
    try:
        await get_redis().zrem(_live_calls_key(project_id), str(call_id))
    except Exception as exc:
        logger.warning("live_call_release_failed call={} error={}", call_id, exc)


@asynccontextmanager
async def hold_live_call(project_id: UUID, call_id: UUID) -> AsyncIterator[None]:
    """Keep `call_id`'s slot for as long as the block runs, then free it.

    Renewing re-adds a lease that lapsed (a long ring, Redis restarted) rather
    than giving up on it: the call is live either way, and the count that
    leaves it out is the wrong one.
    """

    async def _renew() -> None:
        while True:
            await claim_live_call(project_id, call_id, 0)
            await asyncio.sleep(LIVE_CALL_RENEW_SECONDS)

    renewing = asyncio.create_task(_renew())
    try:
        yield
    finally:
        renewing.cancel()
        await asyncio.gather(renewing, return_exceptions=True)
        await release_live_call(project_id, call_id)


def _over_live_calls(limits: ProjectLimits) -> RateLimitedError:
    return RateLimitedError(
        f"This project is at its limit of {limits.live_calls} live calls",
        {"limit": "live_calls", "max": limits.live_calls},
    )


async def admit_live_call(
    session: AsyncSession, *, project_id: UUID, call_id: UUID
) -> None:
    """Take a live-call slot for a call an API request starts (a WebRTC
    connect), or raise 429 `rate_limited`."""
    limits = await limits_for_project(session, project_id)
    if not await claim_live_call(project_id, call_id, limits.live_calls):
        raise _over_live_calls(limits)


async def admit_outbound_call(
    session: AsyncSession, *, project_id: UUID, call_id: UUID, from_number: str
) -> None:
    """Admit a dial from `from_number`, or raise 429 `rate_limited`.

    Takes a live-call slot, then a place in the number's calls-per-second
    window; a dial the second refuses gives the first back. A refused dial
    consumes nothing, so a caller backing off is not charged for the attempts
    it was refused — "429 for the excess", and only the excess.
    """
    limits = await limits_for_project(session, project_id)
    if not await claim_live_call(project_id, call_id, limits.live_calls):
        raise _over_live_calls(limits)
    cps = limits.outbound_calls_per_second
    wait_ms = await _admit(_dials_key(from_number), str(call_id), cps, DIAL_WINDOW_MS)
    if wait_ms:
        await release_live_call(project_id, call_id)
        raise RateLimitedError(
            f"{from_number} is at its limit of {cps} outbound calls per second",
            {
                "limit": "outbound_calls_per_second",
                "max": cps,
                "retry_after_ms": wait_ms,
            },
        )


async def may_dial(
    session: AsyncSession, *, project_id: UUID, from_number: str
) -> DialCheck:
    """Would `place_outbound_call` from `from_number` be admitted right now?
    Takes nothing.

    For a campaign worker pacing itself (#229, #233): asking first keeps it
    from tripping the 429. Two dialers can still race between the question and
    the dial, so a `RateLimitedError` from the dial itself means "not yet" too.
    """
    limits = await limits_for_project(session, project_id)
    live = _live_calls_key(project_id)
    if await _admit(live, "", limits.live_calls, LIVE_CALL_LEASE_MS, peek=True):
        return DialCheck(allowed=False, limit="live_calls")
    cps = limits.outbound_calls_per_second
    wait_ms = await _admit(_dials_key(from_number), "", cps, DIAL_WINDOW_MS, peek=True)
    if wait_ms:
        return DialCheck(
            allowed=False, limit="outbound_calls_per_second", retry_after_ms=wait_ms
        )
    return DialCheck(allowed=True)


async def admit_inbound_call(
    session: AsyncSession,
    *,
    project_id: UUID,
    call_id: UUID,
    transport: str,
    from_number: str,
    to_number: str,
    provider_call_id: str,
) -> bool:
    """Take a live-call slot for an inbound call, or record `call.rejected`.

    False means the carrier must refuse the call (busy) without answering:
    answering only to say no bills minutes, and so would queueing it. There is
    no call row — the call never started — so the event goes to webhook
    subscribers with no `call_id`, and the carrier's id in its payload.
    """
    from turncall.events.dispatcher import dispatch_event

    limits = await limits_for_project(session, project_id)
    if await claim_live_call(project_id, call_id, limits.live_calls):
        return True
    payload = {
        "reason": "live_call_limit",
        "limit": limits.live_calls,
        "transport": transport,
        "from_number": from_number,
        "to_number": to_number,
        "provider_call_id": provider_call_id,
    }
    logger.warning("inbound_call_rejected", project_id=str(project_id), **payload)
    try:
        await dispatch_event(
            session,
            project_id=project_id,
            event_type=CallEventType.CALL_REJECTED.value,
            payload=payload,
        )
    except Exception:
        logger.exception("call_rejected_dispatch_error", project_id=str(project_id))
    return False
