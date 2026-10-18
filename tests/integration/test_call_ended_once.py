"""A call ends once, and sends `call.ended` once (#328, #356).

Its pipeline finishing (`finalize_call`), the carrier's status callback and
the end API all end calls. Each read the status, saw the call in progress and
ended it, so two arriving together each started post-call processing, and the
subscriber got `call.ended` twice. `call_repo.mark_call_ended` is a guarded
UPDATE: of any number racing, one ends the call. `call_repo.claim_post_call`
is another: of the paths that may start post-call processing — on a call a
pipeline runs, the pipeline, else whoever ended it, and the sweep behind
both — one does.

Needs a real Postgres (genuinely concurrent transactions); skips without one.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update

from turncall.config.settings import Settings
from turncall.orchestrator.call_session import finalize_call
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import CallRow, ProjectRow
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


async def test_an_ended_call_is_not_reopened(live_call) -> None:
    """A pipeline starting after the carrier ended the call (its stream built
    late) set it back to in progress, and its ending sent `call.ended` again."""
    sessions, call_id = live_call
    async with sessions() as session:
        await call_repo.mark_call_ended(
            session, call_id, status="completed", ended_at=datetime.now(UTC)
        )
        reopened = await call_repo.update_call_status(
            session, call_id, status="in_progress", started_at=datetime.now(UTC)
        )
        await session.commit()

    assert reopened is None
    async with sessions() as session:
        assert (await call_repo.get_call_by_id(session, call_id)).status == "completed"


def _reading_trigger(sessions, seen: list):
    """A stand-in for `trigger_post_call_analysis` that reads the call's status
    as post-call processing would: in a session of its own, right away."""

    def trigger(_factory, call_id, *_args, **_kwargs):
        async def status() -> str:
            async with sessions() as session:
                return (await call_repo.get_call_by_id(session, call_id)).status

        seen.append(asyncio.ensure_future(status()))

    return trigger


async def test_the_end_api_commits_before_post_call_reads_the_call(live_call) -> None:
    """It started post-call processing inside the request's transaction, which
    then read the call as still in progress (#328)."""
    from turncall.services import call_control

    sessions, call_id = live_call
    seen: list[asyncio.Future] = []
    with (
        patch("turncall.storage.database.get_session_factory", lambda: sessions),
        patch(
            "turncall.services.call_analysis_trigger.config_for_call",
            AsyncMock(return_value={}),
        ),
        patch(
            "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
            _reading_trigger(sessions, seen),
        ),
    ):
        async with sessions() as session:
            result = await call_control.end_call(session, call_id, reason="test")
            # Before the request's own commit, as the endpoint returns.
            statuses = [await f for f in seen]
            await session.commit()

    assert result.success, result.message
    assert statuses == ["completed"]


async def test_the_end_api_racing_the_pipeline_ends_the_call_once(live_call) -> None:
    from turncall.services import call_control

    sessions, call_id = live_call
    both_read = asyncio.Barrier(2)
    read = call_repo.get_call_by_id

    async def read_then_wait(*args, **kwargs):
        call = await read(*args, **kwargs)
        await both_read.wait()
        return call

    async def end_api() -> None:
        async with sessions() as session:
            await call_control.end_call(session, call_id, reason="test")
            await session.commit()

    with (
        patch.object(call_repo, "get_call_by_id", read_then_wait),
        patch("turncall.storage.database.get_session_factory", lambda: sessions),
        patch(
            "turncall.services.call_analysis_trigger.config_for_call",
            AsyncMock(return_value={}),
        ),
        patch(
            "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
            MagicMock(),
        ) as trigger,
    ):
        await asyncio.gather(end_api(), finalize_call(sessions, call_id, failed=False))

    trigger.assert_called_once()


async def _attach_media(sessions, call_id) -> None:
    """The call's stream attached: a pipeline runs it (#356)."""
    async with sessions() as session:
        await session.execute(
            update(CallRow)
            .where(CallRow.id == call_id)
            .values(media_attached_at=datetime.now(UTC))
        )
        await session.commit()


async def test_the_end_api_leaves_post_call_to_the_pipeline(live_call) -> None:
    """On a call a pipeline runs, the end API ends the call but leaves
    post-call processing to the pipeline: started then, `call.ended` would
    read the transcript before its last lines are stored (#356)."""
    from turncall.services import call_control

    sessions, call_id = live_call
    await _attach_media(sessions, call_id)
    with (
        patch("turncall.storage.database.get_session_factory", lambda: sessions),
        patch(
            "turncall.services.call_analysis_trigger.config_for_call",
            AsyncMock(return_value={}),
        ),
        patch(
            "turncall.services.call_analysis_trigger.trigger_post_call_analysis",
            MagicMock(),
        ) as trigger,
    ):
        async with sessions() as session:
            result = await call_control.end_call(session, call_id, reason="test")
            await session.commit()
        assert result.success, result.message
        trigger.assert_not_called()

        await finalize_call(sessions, call_id, failed=False)
        await finalize_call(sessions, call_id, failed=False)

    trigger.assert_called_once()
    async with sessions() as session:
        call = await call_repo.get_call_by_id(session, call_id)
    assert call.status == "completed" and call.post_call_started_at is not None


async def _end_long_ago(sessions, call_id, *, seconds: int) -> None:
    async with sessions() as session:
        await call_repo.mark_call_ended(
            session,
            call_id,
            status="completed",
            ended_at=datetime.now(UTC) - timedelta(seconds=seconds),
        )
        await session.commit()


async def _sweep(sessions) -> list:
    """Sweep as RACERS replicas at once; the calls it started post-call
    processing for. Other tests' calls may be among them: count only yours."""
    from turncall.services import call_analysis_trigger

    with (
        patch.object(
            call_analysis_trigger, "config_for_call", AsyncMock(return_value={})
        ),
        patch.object(
            call_analysis_trigger, "trigger_post_call_analysis", MagicMock()
        ) as trigger,
    ):
        await asyncio.gather(
            *(
                call_analysis_trigger.sweep_unstarted(sessions, now=datetime.now(UTC))
                for _ in range(RACERS)
            )
        )
    return [c.args[1] for c in trigger.call_args_list]


async def test_the_sweep_starts_post_call_a_dead_pipeline_never_did(
    live_call,
) -> None:
    """The carrier ended a call whose pipeline's process then died: nothing
    else would ever send its `call.ended` (#356). Every replica sweeps; one
    starts it."""
    from turncall.services.call_analysis_trigger import SWEEP_GRACE_SECONDS

    sessions, call_id = live_call
    await _attach_media(sessions, call_id)
    await _end_long_ago(sessions, call_id, seconds=SWEEP_GRACE_SECONDS + 5)

    assert (await _sweep(sessions)).count(call_id) == 1
    assert call_id not in await _sweep(sessions)  # and never again


async def test_the_sweep_gives_the_pipeline_its_grace(live_call) -> None:
    sessions, call_id = live_call
    await _attach_media(sessions, call_id)
    await _end_long_ago(sessions, call_id, seconds=5)

    assert call_id not in await _sweep(sessions)


async def test_the_sweep_leaves_a_call_no_pipeline_ran(live_call) -> None:
    """Whoever ended it started its post-call processing, or it sends none
    (unanswered): either way not the sweep's."""
    from turncall.services.call_analysis_trigger import SWEEP_GRACE_SECONDS

    sessions, call_id = live_call
    await _end_long_ago(sessions, call_id, seconds=SWEEP_GRACE_SECONDS + 5)

    assert call_id not in await _sweep(sessions)
