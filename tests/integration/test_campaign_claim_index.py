"""The claim and the janitor stay index lookups as a campaign grows.

`claim_next_target` runs once per dial and `reclaim_abandoned` once per worker
pass, so a scan in either is paid over and over: a claim that walks every
deferred target ahead of the first due one is quadratic over a campaign's
life, and a janitor with no index reads the whole table every couple of
seconds. Measured on a real Postgres with `EXPLAIN`, at a size where the
planner's choice is the one production gets.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from turncall.config.settings import Settings
from turncall.domain.enums import CampaignStatus, CampaignTargetStatus
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import CampaignRow, CampaignTargetRow, ProjectRow
from turncall.storage.repositories import campaign_repo

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
DEFERRED = 4000


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [plan, *(n for child in plan.get("Plans", []) for n in _nodes(child))]


def _scans_of_targets(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sequential scans of `campaign_targets` (the campaign row's own lookup
    is a scan of a table this small, and should be)."""
    return [
        n
        for n in nodes
        if n["Node Type"] == "Seq Scan" and n["Relation Name"] == "campaign_targets"
    ]


async def _plan(session: Any, statement: Any, *, analyze: bool) -> list[dict]:
    """`EXPLAIN` the statement exactly as the repository builds it."""
    connection = await session.connection()
    compiled = statement.compile(dialect=connection.dialect)
    # `updated_at` is an onupdate the ORM fills in at execution.
    values = {**compiled.params, "updated_at": NOW}
    params = tuple(values[name] for name in compiled.positiontup)
    options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    result = await connection.exec_driver_sql(
        f"EXPLAIN ({options}) {compiled}", params
    )
    plan = result.scalar_one()
    plan = json.loads(plan) if isinstance(plan, str) else plan
    return _nodes(plan[0]["Plan"])


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield session_factory
    await engine.dispose()


@pytest.fixture
async def campaign(factory):
    """A running campaign: thousands of targets deferred into the future
    ahead of one that was never dialled, and one deferred target now due."""
    async with factory() as session:
        project = ProjectRow(name=f"claim-index-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.flush()
        row = CampaignRow(
            project_id=project.id,
            name="big",
            from_number_id=uuid.uuid4(),
            status=CampaignStatus.RUNNING.value,
        )
        session.add(row)
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO campaign_targets (id, campaign_id, position, "
                "e164_number, variables, timezone, status, attempts, "
                "next_attempt_at, updated_at) "
                "SELECT gen_random_uuid(), :cid, n, '+1555' || lpad(n::text, 7, '0'),"
                " '{}', 'UTC', 'pending', 0, :later, now() "
                "FROM generate_series(0, :last) AS n"
            ),
            {"cid": row.id, "later": NOW + timedelta(hours=8), "last": DEFERRED - 1},
        )
        await session.execute(
            text(
                "INSERT INTO campaign_targets (id, campaign_id, position, "
                "e164_number, variables, timezone, status, attempts, "
                "next_attempt_at, updated_at) VALUES "
                "(gen_random_uuid(), :cid, :fresh, '+15559990001', '{}', 'UTC', "
                "'pending', 0, NULL, now()), "
                "(gen_random_uuid(), :cid, :due, '+15559990002', '{}', 'UTC', "
                "'pending', 1, :due_at, now())"
            ),
            {
                "cid": row.id,
                "fresh": DEFERRED,
                "due": DEFERRED + 1,
                "due_at": NOW - timedelta(minutes=1),
            },
        )
        await session.commit()
        await session.execute(text("ANALYZE campaign_targets"))
    yield row.id
    async with factory() as session:
        await session.execute(
            text("DELETE FROM projects WHERE id = :id"), {"id": project.id}
        )
        await session.commit()


async def test_a_due_target_is_claimed_before_one_never_dialled(factory, campaign):
    """A retry or a deferral whose time has come goes first — its time is
    the schedule's promise — then the list in order."""
    async with factory() as session:
        claimed = [
            await campaign_repo.claim_next_target(session, campaign, NOW)
            for _ in range(3)
        ]
        numbers = [t.e164_number if t else None for t in claimed]
        await session.rollback()
    assert numbers == ["+15559990002", "+15559990001", None]


async def test_the_claim_does_not_walk_the_deferred_targets(factory, campaign):
    async with factory() as session:
        due = campaign_repo.claim_statement(campaign, NOW)
        nodes = await _plan(session, due, analyze=True)
        await session.rollback()
    scanned = sum(n.get("Rows Removed by Filter", 0) for n in nodes)
    assert _scans_of_targets(nodes) == []
    assert scanned < 10, f"the claim filtered {scanned} rows to find one"


async def test_the_janitor_reads_only_claimed_targets(factory, campaign):
    stale = NOW - timedelta(minutes=2)
    async with factory() as session:
        for status in (CampaignTargetStatus.DIALLING, CampaignTargetStatus.PLACING):
            query = select(CampaignTargetRow.id).where(
                CampaignTargetRow.status == status.value,
                CampaignTargetRow.claimed_at < stale,
            )
            nodes = await _plan(session, query, analyze=False)
            assert _scans_of_targets(nodes) == [], status
