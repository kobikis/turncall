"""One pass of the campaign worker over every campaign with work left (#229).

A pass, per campaign: settle the targets whose calls have ended, then claim
and dial due targets for as long as the project's limits admit another call,
then close the campaign once no target is open. Everything it knows comes from
the database at the start of the pass, so a worker restarted mid-campaign
carries on where the rows say, and two workers share a campaign through the
claim rather than through memory.

Every dial goes through `services/outbound_dial.place_outbound_call`, so the
suppression list and the limits apply exactly as they do to a single API dial:
a suppressed number is refused before the carrier hears of it.

Pacing (#233) is a reservation, not a question: before claiming a target the
pass takes the dial's live-call slot and its place in the number's
calls-per-second window under the call id it will dial with
(`limits.reserve_dial`). The dial's own admission then finds them already held,
so a campaign is never refused by a limit — a question that took nothing would
leave the slot to whichever dialler (another worker, an API dial) got there
first. A "no" ends the campaign's pass, and a rate limit's `retry_after_ms`
tells the worker how soon to come back, so a campaign dials *at* the number's
rate rather than once a poll. With no rate set on the project, a campaign still
paces at `CAMPAIGN_DEFAULT_CALLS_PER_SECOND`.

Time comes from a `Clock`, never `datetime.now()` inline: calling windows
(#230) and retry spacing (#231) are decided against it, and a test has to be
able to say what time it is. The window is checked on the claimed target
itself, right before its dial, so no route to `pending` — a first attempt, a
retry, a reclaim — can reach the carrier outside it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from loguru import logger

from turncall.api.errors import ApiError, ErrorCode, RateLimitedError
from turncall.campaigns import lifecycle
from turncall.campaigns.schedule import CallingWindow, next_open, retry_at
from turncall.config import get_settings
from turncall.domain.enums import CampaignStatus, CampaignTargetStatus
from turncall.services import limits
from turncall.services.call_view import call_view_fields
from turncall.services.outbound_dial import place_outbound_call
from turncall.storage.models import CampaignRow, CampaignTargetRow
from turncall.storage.repositories import campaign_repo, phone_number_repo

Clock = Callable[[], datetime]
SessionFactory = Callable[[], Any]


def system_clock() -> datetime:
    return datetime.now(UTC)


async def run_pass(session_factory: SessionFactory, *, clock: Clock) -> float | None:
    """Advance every campaign with work left. One campaign's fault is logged
    and leaves the others running.

    Returns the seconds until the soonest rate limit that stopped a campaign
    frees up, or None when none did (only a call ending, or time passing, makes
    more work).
    """
    async with session_factory() as session:
        campaigns = await campaign_repo.active_campaigns(session)
    waits: list[float] = []
    for campaign in campaigns:
        try:
            wait = await advance(session_factory, campaign, clock=clock)
        except Exception:
            logger.exception("campaign_pass_error", campaign_id=str(campaign.id))
            continue
        if wait is not None:
            waits.append(wait)
    return min(waits, default=None)


async def advance(
    session_factory: SessionFactory, campaign: CampaignRow, *, clock: Clock
) -> float | None:
    """Settle, dial what the limits admit, and close the campaign when done.

    Returns the seconds until the rate limit that stopped it frees, if one did.
    """
    await _settle(session_factory, campaign, clock)
    async with session_factory() as session:
        if campaign.status == CampaignStatus.QUEUED.value:
            await lifecycle.start(session, campaign.id, clock())
        number = await phone_number_repo.get_by_id(
            session, campaign.from_number_id, project_id=campaign.project_id
        )
        await session.commit()
    # A number deleted since: each dial then fails naming it, target by target.
    from_number = number.e164_number if number is not None else ""
    while (
        go := await _dial_next(session_factory, campaign, from_number, clock)
    ).allowed:
        pass
    async with session_factory() as session:
        await lifecycle.finish_if_done(session, campaign.id, clock())
    return go.retry_after_ms / 1000 if go.retry_after_ms else None


async def _settle(
    session_factory: SessionFactory, campaign: CampaignRow, clock: Clock
) -> None:
    """Settle every `calling` target whose call has ended: back to `pending`
    when the retry policy dials that ending again, else `completed`.

    The reason is read off the call itself (`call_view`, the function the call
    read and `call.ended` share), so a target reports the ending the call does.
    A retry's spacing runs from now, the pass that saw the ending, and its
    instant is moved into the calling window here so `next_attempt_at` reads
    true; the window is still checked again at the dial.
    """
    async with session_factory() as session:
        calling = await campaign_repo.calling_targets(session, campaign.id)
        if not calling:
            return
        fields = await call_view_fields(session, [call for _, call in calling])
        window = CallingWindow.from_json(campaign.calling_window)
        now = clock()
        for target, call in calling:
            reason = fields[call.id]["ended_reason"]
            if reason is None:
                continue
            at = retry_at(
                campaign.retry_policy,
                ended_reason=reason,
                attempts=target.attempts,
                now=now,
                timezone=target.timezone,
                window=window,
            )
            if at is not None:
                await campaign_repo.retry_target(
                    session, target.id, ended_reason=reason, at=at
                )
            else:
                await campaign_repo.finish_target(
                    session,
                    target.id,
                    CampaignTargetStatus.COMPLETED,
                    ended_reason=reason,
                )
        await session.commit()


async def _dial_next(
    session_factory: SessionFactory,
    campaign: CampaignRow,
    from_number: str,
    clock: Clock,
) -> limits.DialCheck:
    """Reserve, claim and dial one due target. `allowed` says whether this
    pass may go on; a refusal carries the limit's `retry_after_ms`."""
    call_id = uuid4()
    async with session_factory() as session:
        check = await limits.reserve_dial(
            session,
            project_id=campaign.project_id,
            call_id=call_id,
            from_number=from_number,
            default_calls_per_second=get_settings().campaigns.default_calls_per_second,
        )
        if not check.allowed:
            return check
        now = clock()
        target = await campaign_repo.claim_next_target(session, campaign.id, now)
        deferred = target is not None and await _defer_outside_window(
            session, campaign, target, now
        )
        # Committed before the dial: a crash mid-dial leaves `dialling`.
        await session.commit()
    if target is None or deferred:
        # Nothing dialled under this reservation: its live-call slot and its
        # place in the number's window go back. A deferred target was only
        # moved, so the pass goes on to the next one.
        await limits.release_dial(campaign.project_id, from_number, call_id)
        return limits.DialCheck(allowed=target is not None)
    dialled = await _dial(session_factory, campaign, target, call_id)
    if not dialled:
        await limits.release_dial(campaign.project_id, from_number, call_id)
    return limits.DialCheck(allowed=True)


async def _defer_outside_window(
    session: Any, campaign: CampaignRow, target: CampaignTargetRow, now: datetime
) -> bool:
    """Outside its calling window: back to `pending` until it opens, in the
    same transaction as the claim, so the deferral is the only trace."""
    opens = next_open(
        now, target.timezone, CallingWindow.from_json(campaign.calling_window)
    )
    if opens <= now:
        return False
    await campaign_repo.defer(session, target.id, opens)
    return True


async def _dial(
    session_factory: SessionFactory,
    campaign: CampaignRow,
    target: CampaignTargetRow,
    call_id: UUID,
) -> bool:
    """Dial a claimed target under its reserved `call_id`, and record what
    became of it. Whether a call was placed."""
    async with session_factory() as session:
        try:
            call = await place_outbound_call(
                session,
                project_id=campaign.project_id,
                # Its own, assigned at create by the campaign's weights (#234).
                agent_id=target.agent_id or campaign.agent_id,
                from_number_id=campaign.from_number_id,
                to_number=target.e164_number,
                metadata=_call_metadata(campaign, target),
                call_id=call_id,
            )
            await campaign_repo.record_dial(session, target.id, call.id)
            # The call row and the target's move commit together.
            await session.commit()
            return True
        except RateLimitedError:
            # Only if the reservation lapsed before the dial (a stall of over
            # a second) and another dialler filled the window meanwhile.
            await session.rollback()
            await campaign_repo.unclaim(session, target.id)
            await session.commit()
            logger.warning("campaign_reservation_lapsed", target_id=str(target.id))
            return False
        except Exception as exc:
            await session.rollback()
            status, error = _refusal(exc)
            await campaign_repo.finish_target(session, target.id, status, error=error)
            await session.commit()
            logger.warning(
                "campaign_target_not_dialled",
                campaign_id=str(campaign.id),
                target_id=str(target.id),
                status=status.value,
                error=error,
            )
            return False


def _refusal(exc: Exception) -> tuple[CampaignTargetStatus, str]:
    """What a refused dial makes of its target."""
    if isinstance(exc, ApiError):
        if exc.code == ErrorCode.NUMBER_SUPPRESSED:
            return CampaignTargetStatus.SUPPRESSED, exc.message
        return CampaignTargetStatus.FAILED, f"{exc.code.value}: {exc.message}"
    return CampaignTargetStatus.FAILED, f"{type(exc).__name__}: {exc}"


def _call_metadata(campaign: CampaignRow, target: CampaignTargetRow) -> dict[str, Any]:
    """The call's metadata: which campaign and target placed it, and the
    target's variables where the media stream renders call-init's from."""
    metadata: dict[str, Any] = {
        "campaign_id": str(campaign.id),
        "campaign_target_id": str(target.id),
    }
    if target.variables:
        metadata["template_variables"] = dict(target.variables)
    return metadata
