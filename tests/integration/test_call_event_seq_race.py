"""Regression: concurrent call_event writes must not collide on sequence_number.

get_next_sequence_number does MAX(seq)+1 then a separate INSERT. Without the
per-call advisory lock added in call_repo, concurrent writers (transcript taps,
handoff + lifecycle events) read the same MAX and violate the
(call_id, sequence_number) unique constraint — silently dropping an event.

This needs a real Postgres (advisory locks + genuine concurrent transactions
can't be mocked or run on sqlite). Skips if the DB isn't reachable.
"""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from turncall.config.settings import Settings
from turncall.domain.enums import CallEventType
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration

CONCURRENCY = 8


async def _db_reachable(session_factory) -> bool:
    try:
        async with session_factory() as session:
            await session.connection()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_concurrent_call_events_get_distinct_sequence_numbers() -> None:
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)

    if not await _db_reachable(session_factory):
        pytest.skip("Postgres not reachable")

    # Set up a project + call to satisfy the FK chain.
    async with session_factory() as session:
        project = ProjectRow(name="seq-race-test")
        session.add(project)
        await session.flush()
        call = await call_repo.create_call(
            session, project_id=project.id, direction="inbound"
        )
        await session.commit()
        call_id = call.id
        project_id = project.id

    async def write_one(i: int) -> int:
        # Mirrors the real path: fresh session, seq + insert + commit in one txn.
        async with session_factory() as session:
            seq = await call_repo.get_next_sequence_number(session, call_id)
            await call_repo.create_call_event(
                session,
                call_id=call_id,
                event_type=CallEventType.TRANSCRIPT_FINAL,
                payload={"i": i},
                sequence_number=seq,
            )
            await session.commit()
            return seq

    try:
        results = await asyncio.gather(
            *[write_one(i) for i in range(CONCURRENCY)]
        )
    except IntegrityError:  # pragma: no cover - this is the bug we're guarding
        pytest.fail("sequence_number collision under concurrency")
    finally:
        # CASCADE cleans up call + events.
        async with session_factory() as session:
            obj = await session.get(ProjectRow, project_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()

    assert len(set(results)) == CONCURRENCY, f"duplicate sequences: {sorted(results)}"
    assert sorted(results) == list(range(1, CONCURRENCY + 1))
