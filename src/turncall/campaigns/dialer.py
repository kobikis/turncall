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

A worker can die anywhere in a dial, and a target says where: `dialling` (claimed,
the carrier not yet asked) or `placing` (the carrier being asked). The janitor
at the top of each pass sends a dead `dialling` claim back to `pending` and ends
a dead `placing` one `failed` — see `campaign_repo.reclaim_abandoned` for why
that one is never dialled again. Pause and cancel act through the same rows: a
paused or cancelled campaign's targets are not claimed, and a claim made just
before loses `begin_placing`, so the carrier never hears of it.

Time comes from a `Clock`, never `datetime.now()` inline: calling windows
(#230) and retry spacing (#231) are decided against it, and a test has to be
able to say what time it is.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from turncall.api.errors import ApiError, ErrorCode, RateLimitedError
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


_DIALLING = (CampaignStatus.QUEUED.value, CampaignStatus.RUNNING.value)


class _NotPlacing(Exception):
    """`begin_placing` refused: this worker must not ask the carrier."""


async def run_pass(session_factory: SessionFactory, *, clock: Clock) -> None:
    """Reclaim dead claims, then advance every campaign with work left. One
    campaign's fault is logged and leaves the others running."""
    await _reclaim(session_factory, clock)
    async with session_factory() as session:
        campaigns = await campaign_repo.active_campaigns(session)
    for campaign in campaigns:
        try:
            await advance(session_factory, campaign, clock=clock)
        except Exception:
            logger.exception("campaign_pass_error", campaign_id=str(campaign.id))


async def _reclaim(session_factory: SessionFactory, clock: Clock) -> None:
    timeout = get_settings().campaigns.claim_timeout_seconds
    async with session_factory() as session:
        repended, unknown = await campaign_repo.reclaim_abandoned(
            session, now=clock(), timeout_seconds=timeout
        )
        await session.commit()
    if repended or unknown:
        logger.warning(
            "campaign_claims_reclaimed", repended=repended, outcome_unknown=unknown
        )


async def advance(
    session_factory: SessionFactory, campaign: CampaignRow, *, clock: Clock
) -> None:
    """Settle, dial what the limits admit, and close the campaign when done.
    A paused or cancelled campaign is only settled."""
    await _settle(session_factory, campaign)
    if campaign.status not in _DIALLING:
        return
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
        target = await campaign_repo.claim_next_target(session, campaign.id, clock())
        # Committed before the dial: a crash mid-dial leaves `dialling`.
        await session.commit()
    if target is None:
        return False
    return await _dial(session_factory, campaign, target, clock)


async def _dial(
    session_factory: SessionFactory,
    campaign: CampaignRow,
    target: CampaignTargetRow,
    clock: Clock,
) -> bool:
    """Dial a claimed target and record what became of it."""

    async def before_carrier() -> None:
        # Its own transaction, committed before the carrier is asked: the
        # dial's session holds the uncommitted call row until afterwards.
        async with session_factory() as placing:
            moved = await campaign_repo.begin_placing(placing, target.id, clock())
            await placing.commit()
        if not moved:
            raise _NotPlacing

    async with session_factory() as session:
        try:
            call = await place_outbound_call(
                session,
                project_id=campaign.project_id,
                agent_id=campaign.agent_id,
                from_number_id=campaign.from_number_id,
                to_number=target.e164_number,
                metadata=_call_metadata(campaign, target),
                before_carrier=before_carrier,
            )
            if not await campaign_repo.record_dial(session, target.id, call.id):
                # The janitor gave up on this dial before the carrier answered:
                # the call is real, the target says `dial_outcome_unknown`.
                logger.error(
                    "campaign_dial_outlived_its_claim",
                    target_id=str(target.id),
                    call_id=str(call.id),
                )
            # The call row and the target's move commit together.
            await session.commit()
            return True
        except _NotPlacing:
            # Paused or cancelled since the claim, or the janitor took it back.
            # Hand it back if it is still `dialling`; a cancelled or
            # re-claimed target is left as it is.
            await session.rollback()
            await campaign_repo.unclaim(session, target.id)
            await session.commit()
            return False
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
