"""One pass of the campaign worker over every campaign with work left (#229).

A pass, per campaign: settle the targets whose calls have ended, then claim
and dial due targets for as long as the project's limits admit another call,
then close the campaign once no target is open. Everything it knows comes from
the database at the start of the pass, so a worker restarted mid-campaign
carries on where the rows say, and two workers share a campaign through the
claim rather than through memory.

Every dial goes through `services/outbound_dial.place_outbound_call`, so the
suppression list and the limits apply exactly as they do to a single API dial:
a suppressed number is refused before the carrier hears of it, and the live-call
limit is taken atomically in Redis at the dial, whatever this module asked
first. `limits.may_dial` is asked before claiming only so a full project is not
claimed-and-unclaimed every pass.

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

from loguru import logger

from turncall.api.errors import ApiError, ErrorCode, RateLimitedError
from turncall.campaigns.schedule import CallingWindow, next_open
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


async def run_pass(session_factory: SessionFactory, *, clock: Clock) -> None:
    """Advance every campaign with work left. One campaign's fault is logged
    and leaves the others running."""
    async with session_factory() as session:
        campaigns = await campaign_repo.active_campaigns(session)
    for campaign in campaigns:
        try:
            await advance(session_factory, campaign, clock=clock)
        except Exception:
            logger.exception("campaign_pass_error", campaign_id=str(campaign.id))


async def advance(
    session_factory: SessionFactory, campaign: CampaignRow, *, clock: Clock
) -> None:
    """Settle, dial what the limits admit, and close the campaign when done."""
    await _settle(session_factory, campaign)
    async with session_factory() as session:
        if campaign.status == CampaignStatus.QUEUED.value:
            await campaign_repo.start_campaign(session, campaign.id, clock())
        number = await phone_number_repo.get_by_id(
            session, campaign.from_number_id, project_id=campaign.project_id
        )
        await session.commit()
    # A number deleted since: each dial then fails naming it, target by target.
    from_number = number.e164_number if number is not None else ""
    while await _dial_next(session_factory, campaign, from_number, clock):
        pass
    async with session_factory() as session:
        if await campaign_repo.complete_if_done(session, campaign.id, clock()):
            logger.info("campaign_completed", campaign_id=str(campaign.id))
        await session.commit()


async def _settle(session_factory: SessionFactory, campaign: CampaignRow) -> None:
    """Close every `calling` target whose call has ended, with its reason.

    Read off the call itself (`call_view`, the function the call read and
    `call.ended` share), so a target reports the ending the call does.
    """
    async with session_factory() as session:
        calling = await campaign_repo.calling_targets(session, campaign.id)
        if not calling:
            return
        fields = await call_view_fields(session, [call for _, call in calling])
        for target, call in calling:
            reason = fields[call.id]["ended_reason"]
            if reason is not None:
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
) -> bool:
    """Claim and dial one due target; False when this pass should stop."""
    async with session_factory() as session:
        check = await limits.may_dial(
            session, project_id=campaign.project_id, from_number=from_number
        )
        if not check.allowed:
            return False
        now = clock()
        target = await campaign_repo.claim_next_target(session, campaign.id, now)
        if target is None:
            return False
        opens = next_open(
            now, target.timezone, CallingWindow.from_json(campaign.calling_window)
        )
        if opens > now:
            # Outside its window: back to `pending` until it opens, in the same
            # transaction as the claim, so the deferral is the only trace.
            await campaign_repo.defer(session, target.id, opens)
            await session.commit()
            return True
        # Committed before the dial: a crash mid-dial leaves `dialling`.
        await session.commit()
    return await _dial(session_factory, campaign, target)


async def _dial(
    session_factory: SessionFactory, campaign: CampaignRow, target: CampaignTargetRow
) -> bool:
    """Dial a claimed target and record what became of it."""
    async with session_factory() as session:
        try:
            call = await place_outbound_call(
                session,
                project_id=campaign.project_id,
                agent_id=campaign.agent_id,
                from_number_id=campaign.from_number_id,
                to_number=target.e164_number,
                metadata=_call_metadata(campaign, target),
            )
            await campaign_repo.record_dial(session, target.id, call.id)
            # The call row and the target's move commit together.
            await session.commit()
            return True
        except RateLimitedError:
            # Another dialler took the slot between the question and the dial.
            await session.rollback()
            await campaign_repo.unclaim(session, target.id)
            await session.commit()
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
            return True


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
