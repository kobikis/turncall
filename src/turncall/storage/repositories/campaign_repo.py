"""Campaigns and their targets (#229).

The worker half is a small state machine on `campaign_targets.status`, and
every transition is a guarded UPDATE: it lands only from the state it expects,
so two workers — or a worker and a janitor (#232) — racing one target leave
exactly one winner, the same way `eval_repo.start_run` claims a run.

The one move that matters most is `begin_placing`: it is the only way to the
carrier, so whichever of two claimants lands it is the one that dials, and a
claimant that loses it dials nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CampaignStatus, CampaignTargetStatus
from turncall.storage.models import (
    CallRow,
    CampaignRow,
    CampaignTargetRow,
    ProjectRow,
    utc_now,
)

_OPEN = [s.value for s in CampaignTargetStatus if not s.is_final]

# What the janitor writes on a target whose worker died while asking the
# carrier. The carrier may have rung the phone; no call row says so.
DIAL_OUTCOME_UNKNOWN = (
    "dial_outcome_unknown: the worker stopped while asking the carrier to "
    "place this call, so it may have rung; not dialled again"
)

# --- the API half --------------------------------------------------------------


async def create_campaign(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    agent_id: UUID | None,
    agent_weights: list[dict[str, Any]] | None,
    from_number_id: UUID,
    timezone: str | None,
    calling_window: dict[str, Any] | None,
    retry_policy: dict[str, Any] | None,
    targets: list[dict[str, Any]],
) -> CampaignRow:
    """A campaign and all of its targets, in one flush.

    Each target dict carries `number`, `variables`, its resolved `timezone` and
    its assigned `agent_id`.
    """
    row = CampaignRow(
        project_id=project_id,
        name=name,
        agent_id=agent_id,
        agent_weights=agent_weights,
        from_number_id=from_number_id,
        timezone=timezone,
        calling_window=calling_window,
        retry_policy=retry_policy,
        status=CampaignStatus.QUEUED.value,
    )
    session.add(row)
    await session.flush()
    now = utc_now()
    await session.execute(
        insert(CampaignTargetRow),
        [
            {
                "id": uuid4(),
                "campaign_id": row.id,
                "position": position,
                "e164_number": target["number"],
                "variables": target["variables"],
                "timezone": target["timezone"],
                "agent_id": target["agent_id"],
                "status": CampaignTargetStatus.PENDING.value,
                "attempts": 0,
                "updated_at": now,
            }
            for position, target in enumerate(targets)
        ],
    )
    await session.flush()
    return row


async def get_campaign(
    session: AsyncSession, campaign_id: UUID, *, project_id: UUID
) -> CampaignRow | None:
    return (
        await session.execute(
            select(CampaignRow).where(
                CampaignRow.id == campaign_id, CampaignRow.project_id == project_id
            )
        )
    ).scalar_one_or_none()


async def get_campaign_by_id(
    session: AsyncSession, campaign_id: UUID
) -> CampaignRow | None:
    """Any project's campaign: for the worker, which serves them all."""
    return await session.get(CampaignRow, campaign_id, populate_existing=True)


async def list_campaigns(
    session: AsyncSession, project_id: UUID, *, limit: int, offset: int
) -> tuple[list[CampaignRow], int]:
    """A page of the project's campaigns, newest first, and the total."""
    where = CampaignRow.project_id == project_id
    total = await session.scalar(select(func.count(CampaignRow.id)).where(where))
    rows = await session.execute(
        select(CampaignRow)
        .where(where)
        .order_by(CampaignRow.created_at.desc(), CampaignRow.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), total or 0


async def target_counts(
    session: AsyncSession, campaign_ids: list[UUID]
) -> dict[UUID, dict[str, int]]:
    """Targets per status for each campaign, every status present (0 if none)."""
    counts = {cid: {s.value: 0 for s in CampaignTargetStatus} for cid in campaign_ids}
    if not campaign_ids:
        return counts
    rows = await session.execute(
        select(
            CampaignTargetRow.campaign_id,
            CampaignTargetRow.status,
            func.count(CampaignTargetRow.id),
        )
        .where(CampaignTargetRow.campaign_id.in_(campaign_ids))
        .group_by(CampaignTargetRow.campaign_id, CampaignTargetRow.status)
    )
    for campaign_id, status, n in rows.all():
        counts[campaign_id][status] = n
    return counts


async def list_targets(
    session: AsyncSession,
    campaign_id: UUID,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[CampaignTargetRow], int]:
    """A page of one campaign's targets in list order, and the total."""
    where = [CampaignTargetRow.campaign_id == campaign_id]
    if status is not None:
        where.append(CampaignTargetRow.status == status)
    total = await session.scalar(select(func.count(CampaignTargetRow.id)).where(*where))
    rows = await session.execute(
        select(CampaignTargetRow)
        .where(*where)
        .order_by(CampaignTargetRow.position)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), total or 0


# --- the worker half -----------------------------------------------------------


async def active_campaigns(session: AsyncSession) -> list[CampaignRow]:
    """Every campaign the worker still has something to do for.

    A paused campaign is among them — its live calls still end and are
    settled — and so is a cancelled one until its last live call has.

    A soft-deleted project (ADR-0011) is gone, so its campaigns stop dialling
    the moment it is deleted, not when the purge job gets to it.
    """
    live_call = exists().where(
        CampaignTargetRow.campaign_id == CampaignRow.id,
        CampaignTargetRow.status == CampaignTargetStatus.CALLING.value,
    )
    rows = await session.execute(
        select(CampaignRow)
        .join(ProjectRow, ProjectRow.id == CampaignRow.project_id)
        .where(
            or_(
                CampaignRow.status.in_(
                    [
                        CampaignStatus.QUEUED.value,
                        CampaignStatus.RUNNING.value,
                        CampaignStatus.PAUSED.value,
                    ]
                ),
                and_(CampaignRow.status == CampaignStatus.CANCELLED.value, live_call),
            ),
            ProjectRow.deleted_at.is_(None),
        )
        .order_by(CampaignRow.created_at, CampaignRow.id)
    )
    return list(rows.scalars().all())


async def start_campaign(
    session: AsyncSession, campaign_id: UUID, now: datetime
) -> bool:
    """`queued` -> `running`; whether this call made the move."""
    result = await session.execute(
        update(CampaignRow)
        .where(
            CampaignRow.id == campaign_id,
            CampaignRow.status == CampaignStatus.QUEUED.value,
        )
        .values(status=CampaignStatus.RUNNING.value, started_at=now)
    )
    return bool(result.rowcount)


async def claim_next_target(
    session: AsyncSession, campaign_id: UUID, now: datetime
) -> CampaignTargetRow | None:
    """Claim the first due `pending` target as `dialling`, or None.

    `SKIP LOCKED` lets two workers claim side by side without either waiting
    on, or taking, the other's row. The caller commits the claim before it
    dials, so the claim outlives a crash during the dial. Only a `running`
    campaign's targets are claimed: a pause or cancel stops the next claim.
    """
    due = (
        select(CampaignTargetRow.id)
        .where(
            CampaignTargetRow.campaign_id == campaign_id,
            _campaign_running(campaign_id),
            CampaignTargetRow.status == CampaignTargetStatus.PENDING.value,
            or_(
                CampaignTargetRow.next_attempt_at.is_(None),
                CampaignTargetRow.next_attempt_at <= now,
            ),
        )
        .order_by(CampaignTargetRow.position)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = await session.execute(
        update(CampaignTargetRow)
        .where(CampaignTargetRow.id == due)
        .values(status=CampaignTargetStatus.DIALLING.value, claimed_at=now)
        .returning(CampaignTargetRow)
    )
    return result.scalar_one_or_none()


def _campaign_running(campaign_id: Any) -> Any:
    return exists().where(
        CampaignRow.id == campaign_id,
        CampaignRow.status == CampaignStatus.RUNNING.value,
    )


async def begin_placing(session: AsyncSession, target_id: UUID, now: datetime) -> bool:
    """`dialling` -> `placing`, committed by the caller just before it asks the
    carrier; whether it moved. False means do not dial: the campaign was
    paused or cancelled since the claim, or the janitor took the claim back
    and possibly another worker holds it now.
    """
    result = await session.execute(
        update(CampaignTargetRow)
        .where(
            CampaignTargetRow.id == target_id,
            CampaignTargetRow.status == CampaignTargetStatus.DIALLING.value,
            _campaign_running(CampaignTargetRow.campaign_id),
        )
        .values(
            status=CampaignTargetStatus.PLACING.value,
            claimed_at=now,
            updated_at=utc_now(),
        )
    )
    return bool(result.rowcount)


async def record_dial(session: AsyncSession, target_id: UUID, call_id: UUID) -> bool:
    """`placing` -> `calling`: the dial was placed as `call_id`."""
    return await _move(
        session,
        target_id,
        frm=[CampaignTargetStatus.PLACING],
        status=CampaignTargetStatus.CALLING,
        call_id=call_id,
        attempts=CampaignTargetRow.attempts + 1,
        # The previous attempt's; this one's arrives when its call ends.
        ended_reason=None,
        claimed_at=None,
    )


async def defer(session: AsyncSession, target_id: UUID, until: datetime) -> bool:
    """`dialling` -> `pending` until `until`: claimed outside its calling window."""
    return await _move(
        session,
        target_id,
        frm=[CampaignTargetStatus.DIALLING],
        status=CampaignTargetStatus.PENDING,
        next_attempt_at=until,
        claimed_at=None,
    )


async def unclaim(session: AsyncSession, target_id: UUID) -> bool:
    """`dialling` -> `pending`: nothing was dialled (a limit said not yet)."""
    return await _move(
        session,
        target_id,
        frm=[CampaignTargetStatus.DIALLING],
        status=CampaignTargetStatus.PENDING,
        claimed_at=None,
    )


async def retry_target(
    session: AsyncSession, target_id: UUID, *, ended_reason: str, at: datetime
) -> bool:
    """`calling` -> `pending` at `at`: the call ended in a way the retry
    policy dials again."""
    return await _move(
        session,
        target_id,
        frm=[CampaignTargetStatus.CALLING],
        status=CampaignTargetStatus.PENDING,
        ended_reason=ended_reason,
        next_attempt_at=at,
    )


async def finish_target(
    session: AsyncSession,
    target_id: UUID,
    status: CampaignTargetStatus,
    *,
    ended_reason: str | None = None,
    error: str | None = None,
) -> bool:
    """Move a claimed, placing or calling target to a final status."""
    return await _move(
        session,
        target_id,
        frm=[
            CampaignTargetStatus.DIALLING,
            CampaignTargetStatus.PLACING,
            CampaignTargetStatus.CALLING,
        ],
        status=status,
        ended_reason=ended_reason,
        error=error,
        claimed_at=None,
    )


async def _move(
    session: AsyncSession,
    target_id: UUID,
    *,
    frm: list[CampaignTargetStatus],
    status: CampaignTargetStatus,
    **values: Any,
) -> bool:
    result = await session.execute(
        update(CampaignTargetRow)
        .where(
            CampaignTargetRow.id == target_id,
            CampaignTargetRow.status.in_([s.value for s in frm]),
        )
        .values(status=status.value, updated_at=utc_now(), **values)
    )
    return bool(result.rowcount)


async def calling_targets(
    session: AsyncSession, campaign_id: UUID
) -> list[tuple[CampaignTargetRow, CallRow]]:
    """The campaign's targets with a call placed, each with its call row."""
    rows = await session.execute(
        select(CampaignTargetRow, CallRow)
        .join(CallRow, CallRow.id == CampaignTargetRow.call_id)
        .where(
            CampaignTargetRow.campaign_id == campaign_id,
            CampaignTargetRow.status == CampaignTargetStatus.CALLING.value,
        )
    )
    return [(target, call) for target, call in rows.all()]


async def complete_if_done(
    session: AsyncSession, campaign_id: UUID, now: datetime
) -> bool:
    """`running` -> `completed` once no target is open; whether it moved."""
    open_target = exists().where(
        and_(
            CampaignTargetRow.campaign_id == campaign_id,
            CampaignTargetRow.status.in_(_OPEN),
        )
    )
    result = await session.execute(
        update(CampaignRow)
        .where(
            CampaignRow.id == campaign_id,
            CampaignRow.status == CampaignStatus.RUNNING.value,
            ~open_target,
        )
        .values(status=CampaignStatus.COMPLETED.value, completed_at=now)
    )
    return bool(result.rowcount)


async def reclaim_abandoned(
    session: AsyncSession, *, now: datetime, timeout_seconds: int
) -> tuple[int, int]:
    """The janitor (#232): claims older than `timeout_seconds`, whose worker
    is taken to be dead. Returns (back to `pending`, ended `failed`).

    There is no heartbeat, as for the eval worker's runs (ADR-0018): age is
    the only evidence. Safety does not rest on it — a live worker slower than
    the timeout loses `begin_placing` or `record_dial` and dials nothing more
    — only how soon a dead worker's targets move on.

    A `dialling` claim never reached the carrier, so it is dialled again. A
    `placing` one may have: the call row commits only after the carrier
    answers, so a worker that died in between leaves no trace of a call that
    may be ringing. It is ended `failed` with `DIAL_OUTCOME_UNKNOWN` — a
    possible missed call, said out loud, over a possible second one.
    """
    cutoff = now - timedelta(seconds=timeout_seconds)
    stale = CampaignTargetRow.claimed_at < cutoff
    repended = await session.execute(
        update(CampaignTargetRow)
        .where(CampaignTargetRow.status == CampaignTargetStatus.DIALLING.value, stale)
        .values(
            status=CampaignTargetStatus.PENDING.value,
            claimed_at=None,
            updated_at=utc_now(),
        )
    )
    unknown = await session.execute(
        update(CampaignTargetRow)
        .where(CampaignTargetRow.status == CampaignTargetStatus.PLACING.value, stale)
        .values(
            status=CampaignTargetStatus.FAILED.value,
            error=DIAL_OUTCOME_UNKNOWN,
            claimed_at=None,
            updated_at=utc_now(),
        )
    )
    return repended.rowcount or 0, unknown.rowcount or 0


# --- control: pause, resume, cancel (#232) -------------------------------------


async def pause_campaign(session: AsyncSession, campaign_id: UUID) -> bool:
    """`queued`/`running` -> `paused`. Live calls are left alone."""
    return await _set_campaign_status(
        session,
        campaign_id,
        frm=[CampaignStatus.QUEUED, CampaignStatus.RUNNING],
        status=CampaignStatus.PAUSED.value,
    )


async def resume_campaign(session: AsyncSession, campaign_id: UUID) -> bool:
    """`paused` -> where it was: `running`, or `queued` if it never started
    (so its start is still the worker's to record)."""
    return await _set_campaign_status(
        session,
        campaign_id,
        frm=[CampaignStatus.PAUSED],
        status=case(
            (CampaignRow.started_at.is_(None), CampaignStatus.QUEUED.value),
            else_=CampaignStatus.RUNNING.value,
        ),
    )


async def cancel_campaign(session: AsyncSession, campaign_id: UUID) -> bool:
    """End a campaign that has not ended: it becomes `cancelled`, and so does
    every target not yet at the carrier — `pending`, and `dialling` (whose
    worker then loses `begin_placing`). A target being placed or on a call
    keeps going and is settled as usual."""
    moved = await _set_campaign_status(
        session,
        campaign_id,
        frm=[CampaignStatus.QUEUED, CampaignStatus.RUNNING, CampaignStatus.PAUSED],
        status=CampaignStatus.CANCELLED.value,
    )
    if moved:
        await session.execute(
            update(CampaignTargetRow)
            .where(
                CampaignTargetRow.campaign_id == campaign_id,
                CampaignTargetRow.status.in_(
                    [
                        CampaignTargetStatus.PENDING.value,
                        CampaignTargetStatus.DIALLING.value,
                    ]
                ),
            )
            .values(
                status=CampaignTargetStatus.CANCELLED.value,
                claimed_at=None,
                updated_at=utc_now(),
            )
        )
    return moved


async def _set_campaign_status(
    session: AsyncSession,
    campaign_id: UUID,
    *,
    frm: list[CampaignStatus],
    status: Any,
) -> bool:
    result = await session.execute(
        update(CampaignRow)
        .where(
            CampaignRow.id == campaign_id,
            CampaignRow.status.in_([s.value for s in frm]),
        )
        .values(status=status)
    )
    return bool(result.rowcount)
