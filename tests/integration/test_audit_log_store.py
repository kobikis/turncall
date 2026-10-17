"""The audit log's store against a real Postgres (#317, ADR-0024).

Append-only in the database, not only in the API: a trigger refuses every
UPDATE and TRUNCATE, and every DELETE but the audit retention janitor's. The
janitor deletes entries past `AUDIT_LOG_RETENTION_DAYS`, every project's, and
nothing else; unset, it deletes nothing. Every API replica runs it: two at once
skip each other's rows rather than wait on them, and both run to the end. That call retention never touches the
log is `test_retention_janitor.py`. What the API writes and reads is
`test_harness_audit_log.py`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from turncall.config.settings import Settings
from turncall.services import audit_log
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import AuditEntryRow
from turncall.storage.repositories import audit_repo

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
async def db():
    engine = create_engine(Settings().database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield engine, factory
    await engine.dispose()


@pytest.fixture
def factory(db):
    return db[1]


@pytest.fixture
async def projects(factory):
    """Project ids whose entries this test wrote, and afterwards gone: as the
    janitor deletes them, since nothing else may."""
    made: list[uuid.UUID] = []
    yield made
    async with factory() as session:
        await session.execute(
            text("SELECT set_config(:name, 'on', true)"),
            {"name": audit_repo.RETENTION_SETTING},
        )
        await session.execute(
            delete(AuditEntryRow).where(AuditEntryRow.project_id.in_(made))
        )
        await session.commit()


async def _entry(factory, projects, *, age_days: float, project=None) -> uuid.UUID:
    project_id = project or uuid.uuid4()
    if project_id not in projects:
        projects.append(project_id)
    entry_id = uuid.uuid4()
    async with factory() as session:
        await session.run_sync(
            audit_repo.insert_entries,
            [
                {
                    "id": entry_id,
                    "project_id": project_id,
                    "api_key_id": uuid.uuid4(),
                    "actor": "api_key",
                    "resource_type": "agent",
                    "resource_id": str(uuid.uuid4()),
                    "action": "updated",
                    "changes": {"/name": {"before": "a", "after": "b"}},
                    "request_id": entry_id,
                    "created_at": NOW - timedelta(days=age_days),
                }
            ],
        )
        await session.commit()
    return entry_id


async def _kept(factory, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    async with factory() as session:
        rows = await session.execute(
            select(AuditEntryRow.id).where(AuditEntryRow.id.in_(ids))
        )
        return set(rows.scalars())


@pytest.mark.parametrize(
    "statement",
    [
        lambda i: (
            update(AuditEntryRow).where(AuditEntryRow.id == i).values(action="deleted")
        ),
        lambda i: delete(AuditEntryRow).where(AuditEntryRow.id == i),
        lambda _i: text("TRUNCATE audit_log"),
    ],
    ids=["update", "delete", "truncate"],
)
async def test_the_database_refuses_to_change_an_entry(
    factory, projects, statement
) -> None:
    entry_id = await _entry(factory, projects, age_days=0)
    async with factory() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(statement(entry_id))
        await session.rollback()
    assert await _kept(factory, [entry_id]) == {entry_id}


async def test_only_the_janitors_transaction_may_delete(factory, projects) -> None:
    entry_id = await _entry(factory, projects, age_days=0)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config(:name, 'on', true)"),
            {"name": audit_repo.RETENTION_SETTING},
        )
        # Still no UPDATE, janitor or not.
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(
                update(AuditEntryRow)
                .where(AuditEntryRow.id == entry_id)
                .values(action="x")
            )
        await session.rollback()
    async with factory() as session:
        deleted = await audit_repo.delete_older_than(
            session, NOW + timedelta(days=1), batch=10_000
        )
        await session.commit()
    assert deleted >= 1
    assert await _kept(factory, [entry_id]) == set()


async def test_the_janitor_deletes_past_the_retention_only(factory, projects) -> None:
    one = uuid.uuid4()
    old = [await _entry(factory, projects, age_days=40, project=one)]
    old.append(await _entry(factory, projects, age_days=31))  # another project
    young = [
        await _entry(factory, projects, age_days=29, project=one),
        await _entry(factory, projects, age_days=0, project=one),
    ]

    deleted = await audit_log.sweep(factory, days=30, now=NOW)

    assert deleted >= len(old)
    assert await _kept(factory, old + young) == set(young)


async def test_unset_keeps_everything(factory, projects) -> None:
    ancient = await _entry(factory, projects, age_days=36500)
    # The janitor does not even start: it returns, sweeping nothing.
    await asyncio.wait_for(audit_log.run_janitor(factory, days=None, interval=0), 1)
    assert await _kept(factory, [ancient]) == {ancient}


async def test_the_janitor_sweeps_in_batches(factory, projects, monkeypatch) -> None:
    monkeypatch.setattr(audit_log, "BATCH", 2)
    old = [await _entry(factory, projects, age_days=400) for _ in range(5)]
    await audit_log.sweep(factory, days=365, now=NOW)
    assert await _kept(factory, old) == set()


async def test_a_sweep_skips_rows_another_holds_and_does_not_wait(
    factory, projects, monkeypatch
) -> None:
    """Another replica's batch holds its rows: this sweep takes the rest
    rather than blocking on them, and those are left for the holder."""
    monkeypatch.setattr(audit_log, "BATCH", 2)
    held = [await _entry(factory, projects, age_days=400) for _ in range(2)]
    free = [await _entry(factory, projects, age_days=400) for _ in range(3)]
    async with factory() as other:
        await other.execute(
            select(AuditEntryRow.id).where(AuditEntryRow.id.in_(held)).with_for_update()
        )
        await asyncio.wait_for(audit_log.sweep(factory, days=365, now=NOW), 10)
        await other.rollback()
    assert await _kept(factory, held + free) == set(held)


async def test_two_sweeps_at_once_share_the_work(
    factory, projects, monkeypatch
) -> None:
    """Every replica sweeps hourly: two sweeps at once delete every entry
    past the cutoff between them, each once, and neither stops early because
    the other took rows from under it."""
    monkeypatch.setattr(audit_log, "BATCH", 3)
    old = [await _entry(factory, projects, age_days=400) for _ in range(20)]
    young = await _entry(factory, projects, age_days=1)

    counts = await asyncio.wait_for(
        asyncio.gather(
            audit_log.sweep(factory, days=365, now=NOW),
            audit_log.sweep(factory, days=365, now=NOW),
        ),
        30,
    )

    assert await _kept(factory, [*old, young]) == {young}
    assert sum(counts) >= len(old)


_WRITE = re.compile(r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)


async def test_the_janitor_writes_nothing_but_the_audit_log(db, projects) -> None:
    engine, factory = db
    await _entry(factory, projects, age_days=400)
    written: set[str] = set()

    def record(_conn, _cursor, statement, *_args) -> None:
        if match := _WRITE.match(statement):
            written.add(match.group(1).lower())

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        await audit_log.sweep(factory, days=365, now=NOW)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert written == {"audit_log"}


async def test_the_read_is_a_projects_newest_first(factory, projects) -> None:
    """And `request_id` reads back what one request wrote."""
    mine = uuid.uuid4()
    ids = [
        await _entry(factory, projects, age_days=age, project=mine) for age in (3, 1, 2)
    ]
    await _entry(factory, projects, age_days=0)  # another project's
    async with factory() as session:
        rows = await audit_repo.list_entries(session, mine, limit=10)
        first = await audit_repo.list_entries(session, mine, limit=1)
        rest = await audit_repo.list_entries(
            session, mine, limit=10, after=(first[0].created_at, first[0].id)
        )
        (one,) = await audit_repo.list_entries(
            session, mine, request_id=ids[2], limit=10
        )
        count = await session.scalar(
            select(func.count())
            .select_from(AuditEntryRow)
            .where(AuditEntryRow.project_id == mine)
        )
    assert [r.id for r in rows] == [ids[1], ids[2], ids[0]]
    assert [r.id for r in first + rest] == [r.id for r in rows]
    assert count == 3
    assert one.id == ids[2]
