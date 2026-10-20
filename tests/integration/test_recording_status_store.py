"""The recorder's `in_progress` cannot take back a final recording status (#367).

On pipecat 1.13 a pipeline cancelled while it starts can cancel the recorder
mid-way through writing `in_progress`, after its cleanup has already ended the
recording. The late write must not land: `call.ended` would then wait out its
whole recording bound for a recording that is over.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import CallRow, ProjectRow
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration


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
async def call_id(factory):
    async with factory() as db:
        project = ProjectRow(name=f"recording-{uuid.uuid4().hex[:6]}")
        db.add(project)
        await db.flush()
        call = CallRow(
            project_id=project.id,
            direction="inbound",
            from_number="+15550001234",
            to_number="+15550009999",
            status="in_progress",
        )
        db.add(call)
        await db.commit()
        project_id, id_ = project.id, call.id
    yield id_
    async with factory() as db:
        await db.execute(delete(ProjectRow).where(ProjectRow.id == project_id))
        await db.commit()


async def _set(factory, call_id, status: str, **kw) -> str:
    async with factory() as db:
        await call_repo.update_call_recording_status(db, call_id, status, **kw)
        await db.commit()
        return await db.scalar(
            select(CallRow.recording_status).where(CallRow.id == call_id)
        )


async def test_in_progress_lands_only_on_a_recording_not_yet_started(
    factory, call_id
) -> None:
    assert await _set(factory, call_id, "in_progress", only_from="none") == (
        "in_progress"
    )


async def test_a_late_in_progress_leaves_a_finished_recording_alone(
    factory, call_id
) -> None:
    assert await _set(factory, call_id, "failed") == "failed"
    assert await _set(factory, call_id, "in_progress", only_from="none") == "failed"
