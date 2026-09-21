"""Eval scenario and run repository (#68)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import EvalRunStatus
from turncall.storage.models import EvalRunRow, EvalScenarioRow


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- scenarios ---------------------------------------------------------------


async def create_scenario(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    kind: str,
    definition: dict[str, Any],
    schema_version: str,
    description: str | None = None,
    tool_mocks: dict[str, Any] | None = None,
    tool_policy: str = "mock_only",
    tags: list[str] | None = None,
    default_target: dict[str, Any] | None = None,
) -> EvalScenarioRow:
    row = EvalScenarioRow(
        project_id=project_id,
        name=name,
        kind=kind,
        definition=definition,
        schema_version=schema_version,
        description=description,
        tool_mocks=tool_mocks or {},
        tool_policy=tool_policy,
        tags=tags or [],
        default_target=default_target,
    )
    session.add(row)
    await session.flush()
    return row


async def get_scenario(
    session: AsyncSession, scenario_id: UUID, *, project_id: UUID | None = None
) -> EvalScenarioRow | None:
    query = select(EvalScenarioRow).where(EvalScenarioRow.id == scenario_id)
    if project_id is not None:
        query = query.where(EvalScenarioRow.project_id == project_id)
    return (await session.execute(query)).scalar_one_or_none()


async def get_scenario_by_name(
    session: AsyncSession, project_id: UUID, name: str
) -> EvalScenarioRow | None:
    return (
        await session.execute(
            select(EvalScenarioRow).where(
                EvalScenarioRow.project_id == project_id,
                EvalScenarioRow.name == name,
            )
        )
    ).scalar_one_or_none()


async def list_scenarios(
    session: AsyncSession,
    project_id: UUID,
    *,
    kind: str | None = None,
    tag: str | None = None,
) -> list[EvalScenarioRow]:
    query = select(EvalScenarioRow).where(EvalScenarioRow.project_id == project_id)
    if kind is not None:
        query = query.where(EvalScenarioRow.kind == kind)
    if tag is not None:
        # `tags @> ARRAY[tag]` — the containment operator the GIN index serves.
        query = query.where(EvalScenarioRow.tags.contains([tag]))
    query = query.order_by(EvalScenarioRow.created_at.desc())
    return list((await session.execute(query)).scalars().all())


async def update_scenario(
    session: AsyncSession, scenario_id: UUID, *, values: dict[str, Any]
) -> EvalScenarioRow:
    await session.execute(
        update(EvalScenarioRow)
        .where(EvalScenarioRow.id == scenario_id)
        .values(**values, updated_at=_utc_now())
    )
    await session.flush()
    row = await get_scenario(session, scenario_id)
    if row is None:  # pragma: no cover - the caller just read this row
        raise LookupError(f"eval scenario {scenario_id} vanished during update")
    return row


async def delete_scenario(session: AsyncSession, scenario_id: UUID) -> None:
    """Delete a scenario. Its runs survive with `scenario_id` set to NULL —
    deleting the test must not destroy the record of what it once proved."""
    row = await get_scenario(session, scenario_id)
    if row is not None:
        await session.delete(row)
        await session.flush()


# --- runs --------------------------------------------------------------------


async def create_run(
    session: AsyncSession,
    *,
    project_id: UUID,
    scenario_id: UUID | None,
    scenario_name: str,
    kind: str,
    target: dict[str, Any],
    resolved_scenario: dict[str, Any],
    modality: str,
    iterations: int,
    batch_id: UUID | None = None,
) -> EvalRunRow:
    """Queue a run. The agent config and harness snapshots are written by the
    worker when it claims the run — they describe what actually ran, and the
    agent may be edited between queueing and starting."""
    row = EvalRunRow(
        project_id=project_id,
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        kind=kind,
        target=target,
        resolved_config={},
        resolved_scenario=resolved_scenario,
        harness_config={},
        modality=modality,
        iterations=iterations,
        batch_id=batch_id,
        status=EvalRunStatus.QUEUED.value,
    )
    session.add(row)
    await session.flush()
    return row


async def get_run(
    session: AsyncSession, run_id: UUID, *, project_id: UUID | None = None
) -> EvalRunRow | None:
    query = select(EvalRunRow).where(EvalRunRow.id == run_id)
    if project_id is not None:
        query = query.where(EvalRunRow.project_id == project_id)
    return (await session.execute(query)).scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    project_id: UUID,
    *,
    batch_id: UUID | None = None,
    scenario_id: UUID | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[EvalRunRow]:
    query = select(EvalRunRow).where(EvalRunRow.project_id == project_id)
    if batch_id is not None:
        query = query.where(EvalRunRow.batch_id == batch_id)
    if scenario_id is not None:
        query = query.where(EvalRunRow.scenario_id == scenario_id)
    if status is not None:
        query = query.where(EvalRunRow.status == status)
    query = query.order_by(EvalRunRow.queued_at.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


async def start_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    resolved_config: dict[str, Any],
    agent_id: UUID | None,
    harness_config: dict[str, Any],
    agent_version: int | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> None:
    """Claim a queued run, recording the two snapshots only now knowable.

    The WHERE on `queued` is the claim: two workers racing the same run leave
    exactly one update, so the loser's `execute_run` finds it no longer queued.
    """
    await session.execute(
        update(EvalRunRow)
        .where(EvalRunRow.id == run_id, EvalRunRow.status == EvalRunStatus.QUEUED.value)
        .values(
            status=EvalRunStatus.RUNNING.value,
            started_at=_utc_now(),
            resolved_config=resolved_config,
            agent_id=agent_id,
            agent_version=agent_version,
            harness_config=harness_config,
            # Written with the snapshots because they are known at the same
            # moment and describe the same thing: what this run actually is.
            warnings=warnings or [],
        )
    )
    await session.flush()


async def finish_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: EvalRunStatus,
    passed_count: int,
    failed_count: int,
    results: list[dict[str, Any]],
    error: str | None = None,
) -> None:
    await session.execute(
        update(EvalRunRow)
        .where(EvalRunRow.id == run_id)
        .values(
            status=status.value,
            passed_count=passed_count,
            failed_count=failed_count,
            results=results,
            error=error,
            completed_at=_utc_now(),
        )
    )
    await session.flush()


async def cancel_run(session: AsyncSession, run_id: UUID) -> bool:
    """Cancel a run that has not finished. Returns whether anything changed."""
    result = await session.execute(
        update(EvalRunRow)
        .where(
            EvalRunRow.id == run_id,
            EvalRunRow.status.in_(
                [EvalRunStatus.QUEUED.value, EvalRunStatus.RUNNING.value]
            ),
        )
        .values(status=EvalRunStatus.CANCELLED.value, completed_at=_utc_now())
    )
    await session.flush()
    return bool(getattr(result, "rowcount", 0))


async def fail_if_still_queued(
    session: AsyncSession, run_id: UUID, *, error: str
) -> bool:
    """Mark a run `errored` only while it is still queued.

    The guard is the point. A Redis push can raise *after* the write landed —
    the reply read times out — in which case a worker may already have claimed
    the run. An unconditional update would stomp a run that is legitimately in
    flight and report a failure that did not happen.
    """
    result = await session.execute(
        update(EvalRunRow)
        .where(EvalRunRow.id == run_id, EvalRunRow.status == EvalRunStatus.QUEUED.value)
        .values(
            status=EvalRunStatus.ERRORED.value,
            completed_at=_utc_now(),
            error=error,
        )
    )
    await session.flush()
    return bool(getattr(result, "rowcount", 0))


async def reclaim_stalled_runs(
    session: AsyncSession, *, max_age_seconds: int, max_queued_seconds: int
) -> int:
    """Sweep runs nothing will ever finish into `errored`.

    Two ways a run is abandoned, and both need a sweep:

    A **claimed** run whose worker crashed. Nothing else would move it, and a
    row stuck at `running` reads to an operator as work still in flight.

    A **queued** run nothing ever picked up — the API committed the row and then
    the process died before the push, or Redis restarted without persistence and
    dropped the list. The endpoint handles the push *raising*, but it cannot
    handle not being alive, so without this the row waits forever while the
    caller was told 202 Accepted. The threshold is separate and longer, because
    a queued run waiting behind a backlog is normal and a claimed one still
    running after an hour is not.

    `errored`, not `failed`, for both: nobody learned anything about the agent.
    """
    from datetime import timedelta

    now = _utc_now()
    result = await session.execute(
        update(EvalRunRow)
        .where(
            or_(
                and_(
                    EvalRunRow.status == EvalRunStatus.RUNNING.value,
                    EvalRunRow.started_at < now - timedelta(seconds=max_age_seconds),
                ),
                and_(
                    EvalRunRow.status == EvalRunStatus.QUEUED.value,
                    EvalRunRow.queued_at < now - timedelta(seconds=max_queued_seconds),
                ),
            )
        )
        .values(
            status=EvalRunStatus.ERRORED.value,
            completed_at=now,
            error="abandoned: no worker finished this run within its time budget",
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
