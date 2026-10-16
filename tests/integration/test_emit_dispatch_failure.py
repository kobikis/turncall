"""A failed send cannot cost the stored row, on real Postgres (#312 review).

The unit half, `tests/unit/test_emit_dispatch_failure.py`, says why. This one
makes a statement fail inside the send on a real session, where Postgres
aborts the transaction unless the exit point's SAVEPOINT is rolled back to,
and checks that the caller's commit still lands the event. Skips if the DB
isn't reachable.
"""

import pytest
from sqlalchemy import text

from turncall.config.settings import Settings
from turncall.events import dispatcher
from turncall.events.emit import emit_call_event, redact
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration


async def test_a_failed_send_leaves_the_stored_row_to_the_callers_commit(
    monkeypatch,
) -> None:
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable")

    async with session_factory() as session:
        project = ProjectRow(name="emit-dispatch-failure-test")
        session.add(project)
        await session.flush()
        call = await call_repo.create_call(
            session, project_id=project.id, direction="inbound"
        )
        await session.commit()
        call_id, project_id = call.id, project.id

    async def failing_read(session, _project_id, _event_type):
        await session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(dispatcher, "get_active_subscribers", failing_read)
    try:
        async with session_factory() as session:
            await emit_call_event(
                session,
                call_id=call_id,
                project_id=project_id,
                event_type="transcript.final",
                payload=await redact(
                    project_id,
                    "transcript.final",
                    {"role": "customer", "text": "hello"},
                    sessions=session_factory,
                ),
            )
            await session.commit()

        async with session_factory() as session:
            events = await call_repo.list_call_events(session, call_id)
        assert [(e.event_type, e.payload) for e in events] == [
            ("transcript.final", {"role": "customer", "text": "hello"})
        ]
    finally:
        async with session_factory() as session:
            obj = await session.get(ProjectRow, project_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()
        await engine.dispose()
