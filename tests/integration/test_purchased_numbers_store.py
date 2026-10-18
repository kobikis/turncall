"""`purchased_number_repo` against a real Postgres (#321, ADR-0025): who
holds a number bought on the platform's shared account.

A number is on the platform's account once (`uq_purchased_numbers_platform_
number`), so the latest purchase takes it over: an earlier project's row is
stale, whether it was committed before the purchase or in a race with it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.domain.carriers import OWN_ACCOUNT, PLATFORM_ACCOUNT
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow, PurchasedNumberRow
from turncall.storage.repositories import purchased_number_repo

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.execute(select(PurchasedNumberRow.id).limit(1))
    except Exception:
        pytest.skip("Postgres with purchased_numbers not reachable")
    created: list[uuid.UUID] = []
    session_factory.created = created  # type: ignore[attr-defined]
    yield session_factory
    async with session_factory() as session:
        await session.execute(delete(ProjectRow).where(ProjectRow.id.in_(created)))
        await session.commit()
    await engine.dispose()


async def _project(factory) -> uuid.UUID:
    async with factory() as session:
        project = ProjectRow(name=f"bought-{uuid.uuid4().hex[:6]}")
        session.add(project)
        await session.commit()
        factory.created.append(project.id)
        return project.id


def _e164() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


async def _holders(factory, e164: str) -> list[tuple[uuid.UUID, str]]:
    async with factory() as session:
        rows = await session.execute(
            select(PurchasedNumberRow.project_id, PurchasedNumberRow.account).where(
                PurchasedNumberRow.e164_number == e164
            )
        )
        return [(r.project_id, r.account) for r in rows]


async def _record(session, project_id, e164, account=PLATFORM_ACCOUNT) -> None:
    await purchased_number_repo.record(
        session,
        project_id=project_id,
        carrier="twilio",
        e164_number=e164,
        account=account,
    )


async def test_a_purchase_takes_over_an_earlier_projects_stale_row(factory) -> None:
    first, second, e164 = await _project(factory), await _project(factory), _e164()
    async with factory() as session:
        await _record(session, first, e164)
        await session.commit()
    async with factory() as session:
        await _record(session, second, e164)
        await session.commit()

    assert await _holders(factory, e164) == [(second, PLATFORM_ACCOUNT)]


async def test_a_racing_purchase_takes_ownership_rather_than_failing(
    factory,
) -> None:
    """Another project's purchase of the number, recording at the same
    time, would land its row between this one's stale-row sweep and its
    insert, violating the platform index. The purchase is real and already
    billed, so it waits for the other to commit, sweeps its row and takes
    ownership instead of failing."""
    earlier, later, e164 = await _project(factory), await _project(factory), _e164()
    async with factory() as racing, factory() as session:
        await _record(racing, earlier, e164)  # not committed yet
        recording = asyncio.create_task(_record(session, later, e164))
        await asyncio.sleep(0.3)  # now waiting on `racing`
        assert not recording.done()
        await racing.commit()
        await asyncio.wait_for(recording, timeout=5)
        await session.commit()

    assert await _holders(factory, e164) == [(later, PLATFORM_ACCOUNT)]


async def test_a_number_on_projects_own_accounts_is_not_taken_over(factory) -> None:
    first, second, e164 = await _project(factory), await _project(factory), _e164()
    async with factory() as session:
        await _record(session, first, e164, OWN_ACCOUNT)
        await _record(session, second, e164, OWN_ACCOUNT)
        await session.commit()

    assert sorted(await _holders(factory, e164)) == sorted(
        [(first, OWN_ACCOUNT), (second, OWN_ACCOUNT)]
    )


async def test_the_numbers_a_project_bought_on_the_platform(factory) -> None:
    """What a purge warns about: the platform-account numbers only."""
    project, shared, own = await _project(factory), _e164(), _e164()
    async with factory() as session:
        await _record(session, project, shared)
        await _record(session, project, own, OWN_ACCOUNT)
        await session.commit()
    async with factory() as session:
        found = await purchased_number_repo.bought_on_platform_by(session, project)

    assert found == [("twilio", shared)]
