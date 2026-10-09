"""A campaign starting and finishing, and the events that say so (#234).

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


async def _announce(
    session: AsyncSession, campaign_id: UUID, event_type: CallEventType
) -> None:
    """Best effort, like every dispatch: the row is written either way, and a
    subscriber being down must not stop a campaign."""
    from turncall.events.dispatcher import dispatch_event

    try:
        row = await campaign_repo.get_campaign_by_id(session, campaign_id)
        if row is None:
            return
        await dispatch_event(
            session,
            project_id=row.project_id,
            event_type=event_type.value,
            payload=await _payload(session, row),
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
