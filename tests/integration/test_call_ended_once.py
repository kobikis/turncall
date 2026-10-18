"""A call ends once, and only that ending sends `call.ended` (#328).

Its pipeline finishing (`finalize_call`), the carrier's status callback and
the end API all end calls. Each read the status, saw the call in progress and
ended it, so two arriving together each started post-call processing, and the
subscriber got `call.ended` twice. `call_repo.mark_call_ended` is a guarded
UPDATE: of any number racing, one ends the call.

Needs a real Postgres (genuinely concurrent transactions); skips without one.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.config.settings import Settings
from turncall.orchestrator.call_session import finalize_call
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow
from turncall.storage.repositories import call_repo

pytestmark = pytest.mark.integration

RACERS = 8


@pytest.fixture
async def live_call():
    """A project and an in-progress call; the session factory they live in."""
    engine = create_engine(Settings().database)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            await session.connection()
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable")

    async with sessions() as session:
        project = ProjectRow(name="call-ended-once")
        session.add(project)
        await session.flush()
        call = await call_repo.create_call(
            session, project_id=project.id, direction="inbound"
        )
        await call_repo.update_call_status(
            session, call.id, status="in_progress", started_at=datetime.now(UTC)
        )
        await session.commit()
        project_id, call_id = project.id, call.id

    yield sessions, call_id

    async with sessions() as session:  # CASCADE takes the call
        project = await session.get(ProjectRow, project_id)
        if project is not None:
            await session.delete(project)
            await session.commit()
    await engine.dispose()


async def test_racing_ends_end_the_call_once(live_call) -> None:
    sessions, call_id = live_call

    async def end() -> bool:
        async with sessions() as session:
            ended = await call_repo.mark_call_ended(
                session, call_id, status="completed", ended_at=datetime.now(UTC)
            )
            await session.commit()
            return ended is not None

    assert sum(await asyncio.gather(*(end() for _ in range(RACERS)))) == 1


async def test_racing_finalizes_start_post_call_processing_once(live_call) -> None:
    sessions, call_id = live_call
    # Every finalize has read the call, in progress, before any ends it: the
    # interleaving that sent `call.ended` twice.
    all_read = asyncio.Barrier(RACERS)
    read = call_repo.get_call_by_id

    async def read_then_wait(*args, **kwargs):
        call = await read(*args, **kwargs)
        await all_read.wait()
        return call

    with (
        patch.object(call_repo, "get_call_by_id", read_then_wait),
        patch(
            "turncall.services.call_analysis_trigger.config_for_call",
            AsyncMock(return_value={}),
        ),
        patch(
            "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
            MagicMock(),
        ) as trigger,
    ):
        await asyncio.gather(
            *(finalize_call(sessions, call_id, failed=False) for _ in range(RACERS))
        )

    trigger.assert_called_once()
    async with sessions() as session:
        call = await call_repo.get_call_by_id(session, call_id)
    assert call.status == "completed" and call.ended_at is not None


async def test_an_ended_call_is_not_ended_again(live_call) -> None:
    """A call the carrier already reported as unanswered stays that way."""
    sessions, call_id = live_call
    async with sessions() as session:
        await call_repo.mark_call_ended(
            session, call_id, status="no_answer", ended_at=datetime.now(UTC)
        )
        await session.commit()

    with patch(
        "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
        MagicMock(),
    ) as trigger:
        await finalize_call(sessions, call_id, failed=False)

    trigger.assert_not_called()
    async with sessions() as session:
        assert (await call_repo.get_call_by_id(session, call_id)).status == "no_answer"
