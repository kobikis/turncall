"""Campaigns: outbound calls one agent places to a list of targets (#229).

The API only records a campaign; `turncall-campaign-worker` dials it, through
the same dial service a single `POST /v1/calls/outbound` uses — so the
suppression list and the project's limits hold for every target.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.campaigns import (
    CampaignResponse,
    CampaignTargetResponse,
    CreateCampaignRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.campaigns import queue as campaign_queue
from turncall.domain.enums import CampaignTargetStatus
from turncall.storage.models import CampaignRow
from turncall.storage.repositories import agent_repo, campaign_repo, phone_number_repo

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post("", status_code=201)
async def create_campaign(
    body: CreateCampaignRequest, auth: WriteAuth, session: DbSession
) -> dict:
    """Record a campaign; the campaign worker starts dialling it."""
    for agent_id in body.agent_ids():
        if (
            await agent_repo.get_agent_by_id(
                session, agent_id, project_id=auth.project_id
            )
            is None
        ):
            raise NotFoundError("Agent", str(agent_id))
    if (
        await phone_number_repo.get_by_id(
            session, body.from_number_id, project_id=auth.project_id
        )
        is None
    ):
        raise NotFoundError("PhoneNumber", str(body.from_number_id))

    row = await campaign_repo.create_campaign(
        session,
        project_id=auth.project_id,
        name=body.name,
        agent_id=body.agent_id,
        agent_weights=body.agent_weights(),
        from_number_id=body.from_number_id,
        timezone=body.timezone,
        targets=body.resolved_targets(),
    )
    counts = await campaign_repo.target_counts(session, [row.id])
    await session.commit()
    await _wake_worker(row.id)
    return ok(CampaignResponse.from_row(row, counts[row.id]))


async def _wake_worker(campaign_id: UUID) -> None:
    """Best effort: the worker polls the database anyway, so a failed push
    delays the first dial by one poll and loses nothing."""
    try:
        from turncall.storage.redis import get_redis

        await campaign_queue.nudge(get_redis(), campaign_id)
    except Exception as exc:
        logger.warning("campaign_nudge_failed id={} error={}", campaign_id, exc)


@router.get("")
async def list_campaigns(
    auth: Auth,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """The project's campaigns, newest first, each with its target counts."""
    rows, total = await campaign_repo.list_campaigns(
        session, auth.project_id, limit=limit, offset=(page - 1) * limit
    )
    counts = await campaign_repo.target_counts(session, [r.id for r in rows])
    return paginated(
        [CampaignResponse.from_row(r, counts[r.id]) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: UUID, auth: Auth, session: DbSession) -> dict:
    row = await _campaign(session, campaign_id, auth.project_id)
    counts = await campaign_repo.target_counts(session, [row.id])
    return ok(CampaignResponse.from_row(row, counts[row.id]))


@router.get("/{campaign_id}/targets")
async def list_campaign_targets(
    campaign_id: UUID,
    auth: Auth,
    session: DbSession,
    status: CampaignTargetStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """Each target's status, in list order; `status` filters to one."""
    await _campaign(session, campaign_id, auth.project_id)
    rows, total = await campaign_repo.list_targets(
        session,
        campaign_id,
        status=status.value if status is not None else None,
        limit=limit,
        offset=(page - 1) * limit,
    )
    return paginated(
        [CampaignTargetResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


async def _campaign(
    session: AsyncSession, campaign_id: UUID, project_id: UUID
) -> CampaignRow:
    row = await campaign_repo.get_campaign(session, campaign_id, project_id=project_id)
    if row is None:
        raise NotFoundError("Campaign", str(campaign_id))
    return row
