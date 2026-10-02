"""The suppression list against a real Postgres (#227).

The add is an `ON CONFLICT` statement and the scoping is in SQL, which only
Postgres can exercise.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow, SuppressedNumberRow
from turncall.storage.repositories import suppression_repo

pytestmark = pytest.mark.integration

N = "+15550000002"


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.execute(select(SuppressedNumberRow).limit(1))
    except Exception:
        pytest.skip("Postgres with suppressed_numbers not reachable")
    created: list[uuid.UUID] = []
    session_factory.created = created  # type: ignore[attr-defined]
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(ProjectRow).where(ProjectRow.id.in_(created)))
        await session.commit()
    await engine.dispose()


async def _project(factory) -> uuid.UUID:
    async with factory() as session:
        project = ProjectRow(name=f"suppr-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.commit()
        factory.created.append(project.id)
        return project.id


async def test_adding_twice_is_one_entry(factory):
    pid = await _project(factory)
    async with factory() as session:
        first = await suppression_repo.add(session, project_id=pid, number=N)
        again = await suppression_repo.add(session, project_id=pid, number=N)
        await session.commit()
        rows, total = await suppression_repo.list_for_project(
            session, pid, limit=10, offset=0
        )
    assert again.id == first.id
    assert total == 1 and [r.id for r in rows] == [first.id]


async def test_one_projects_list_never_touches_another(factory):
    mine, theirs = await _project(factory), await _project(factory)
    async with factory() as session:
        entry = await suppression_repo.add(session, project_id=mine, number=N)
        await session.commit()

        assert await suppression_repo.is_suppressed(session, project_id=mine, number=N)
        assert not await suppression_repo.is_suppressed(
            session, project_id=theirs, number=N
        )
        assert (
            await suppression_repo.list_for_project(session, theirs, limit=10, offset=0)
        )[1] == 0
        assert not await suppression_repo.remove(
            session, project_id=theirs, entry_id=entry.id
        )
        assert await suppression_repo.is_suppressed(session, project_id=mine, number=N)


async def test_removed_is_dialable_again(factory):
    pid = await _project(factory)
    async with factory() as session:
        entry = await suppression_repo.add(session, project_id=pid, number=N)
        await session.commit()
        assert await suppression_repo.remove(session, project_id=pid, entry_id=entry.id)
        await session.commit()
        assert not await suppression_repo.is_suppressed(
            session, project_id=pid, number=N
        )


async def test_list_filters_by_number_and_pages(factory):
    pid = await _project(factory)
    async with factory() as session:
        for n in ("+15550000001", N, "+15550000003"):
            await suppression_repo.add(session, project_id=pid, number=n)
        await session.commit()
        one, total = await suppression_repo.list_for_project(
            session, pid, limit=10, offset=0, number=N
        )
        page, all_total = await suppression_repo.list_for_project(
            session, pid, limit=2, offset=2
        )
    assert total == 1 and one[0].e164_number == N
    assert all_total == 3 and len(page) == 1
