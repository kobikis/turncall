"""A campaign starting, finishing and being cancelled, and the events that say
so (#234, #232).

Each move is a guarded UPDATE in `campaign_repo`, so however many workers
reach it, exactly one makes it — and only that one dispatches the event, after
its commit: an event announcing a state the database does not hold yet is one
a subscriber cannot check. `finish_if_done` is the one place a campaign
finishes; anything that can leave the last target terminal calls it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallEventType
from turncall.storage.repositories import campaign_repo


async def start(session: AsyncSession, campaign_id: UUID, now: datetime) -> bool:
    """`queued` -> `running`, committed, then `campaign.started`."""
    if not await campaign_repo.start_campaign(session, campaign_id, now):
        return False
    await session.commit()
    await _announce(session, campaign_id, CallEventType.CAMPAIGN_STARTED)
    return True


async def finish_if_done(
    session: AsyncSession, campaign_id: UUID, now: datetime
) -> bool:
    """`running` -> `completed` once every target is terminal, committed, then
    `campaign.completed`. Whether this call finished it."""
    if not await campaign_repo.complete_if_done(session, campaign_id, now):
        return False
    await session.commit()
    logger.info("campaign_completed", campaign_id=str(campaign_id))
    await _announce(session, campaign_id, CallEventType.CAMPAIGN_COMPLETED)
    return True


async def cancel(session: AsyncSession, campaign_id: UUID) -> bool:
    """Cancel a campaign that has not ended (and every target not yet at the
    carrier), committed, then `campaign.cancelled`. Whether this call moved it:
    a second cancel finds it `cancelled` already and announces nothing."""
    if not await campaign_repo.cancel_campaign(session, campaign_id):
        return False
    await session.commit()
    logger.info("campaign_cancelled", campaign_id=str(campaign_id))
    await _announce(session, campaign_id, CallEventType.CAMPAIGN_CANCELLED)
    return True


async def _announce(
    session: AsyncSession, campaign_id: UUID, event_type: CallEventType
) -> None:
    """Best effort, like every dispatch: the row is written either way, and a
    subscriber being down must not stop a campaign."""
    from turncall.events.emit import dispatch_project_event, redact

    try:
        row = await campaign_repo.get_campaign_by_id(session, campaign_id)
        if row is None:
            return
        # The campaign's change is committed (every caller commits first), so
        # ending the read here commits nothing while redaction waits.
        payload = await redact(
            row.project_id,
            event_type.value,
            await _payload(session, row),
            release=session,
        )
        await dispatch_project_event(
            session,
            project_id=row.project_id,
            event_type=event_type.value,
            payload=payload,
            campaign_id=row.id,
            agent_id=row.agent_id,
        )
    except Exception:
        logger.exception(
            "campaign_event_dispatch_failed",
            campaign_id=str(campaign_id),
            event=event_type.value,
        )


async def _payload(session: AsyncSession, row: Any) -> dict[str, Any]:
    """The campaign's read — what `GET /v1/campaigns/{id}` answers."""
    from turncall.api.v1.schemas.campaigns import CampaignResponse

    counts = await campaign_repo.target_counts(session, [row.id])
    return CampaignResponse.from_row(row, counts[row.id]).model_dump(
        mode="json", exclude={"id"}
    )
