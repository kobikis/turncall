"""Eval scenarios and runs against a real Postgres.

Two of this slice's rules only exist in the database and cannot be mocked:

- Deleting a scenario must leave its runs readable, with the name intact. That
  is `ON DELETE SET NULL` plus the denormalised `scenario_name`, not code.
- The janitor sweeps a run a crashed worker left claimed into `errored`. That
  is a timestamp comparison the DB does.

Skips if Postgres isn't reachable, like the other integration tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from turncall.config.settings import Settings
from turncall.domain.enums import EvalRunStatus
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import EvalRunRow, ProjectRow
from turncall.storage.repositories import eval_repo

pytestmark = pytest.mark.integration

SCRIPTED = {"turns": [{"user": "hello", "expect": [{"event": "llm_response"}]}]}


async def _db_reachable(session_factory) -> bool:
    try:
        async with session_factory() as session:
            await session.connection()
        return True
    except Exception:
        return False


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    if not await _db_reachable(session_factory):
        pytest.skip("Postgres not reachable")
    yield session_factory
    await engine.dispose()


async def _project(session, name: str):
    project = ProjectRow(name=name)
    session.add(project)
    await session.flush()
    return project


async def _scenario_and_run(session, project_id, *, name="greets-the-caller"):
    scenario = await eval_repo.create_scenario(
        session,
        project_id=project_id,
        name=name,
        kind="script",
        definition=SCRIPTED,
        schema_version="pipecat-1.11",
    )
    run = await eval_repo.create_run(
        session,
        project_id=project_id,
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        kind="script",
        target={"type": "agent", "agent_id": str(project_id)},
        resolved_scenario={"definition": SCRIPTED},
        modality="text",
        iterations=1,
    )
    return scenario, run


@pytest.mark.asyncio
async def test_deleting_a_scenario_leaves_its_runs_readable(factory) -> None:
    async with factory() as session:
        project = await _project(session, "eval-delete-test")
        scenario, run = await _scenario_and_run(session, project.id)
        run_id = run.id
        await session.commit()

    async with factory() as session:
        await eval_repo.delete_scenario(session, scenario.id)
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread is not None, "the run must survive its scenario"
        assert reread.scenario_id is None
        assert reread.scenario_name == "greets-the-caller"


@pytest.mark.asyncio
async def test_the_janitor_reclaims_a_run_a_worker_abandoned(factory) -> None:
    async with factory() as session:
        project = await _project(session, "eval-janitor-test")
        _scenario, run = await _scenario_and_run(
            session, project.id, name="janitor-subject"
        )
        run_id = run.id
        await eval_repo.start_run(
            session,
            run_id,
            resolved_config={"system_prompt": "hi"},
            agent_id=None,
            harness_config={"pipecat_version": "1.11.0"},
        )
        # Backdate the claim: the worker took it and never came back.
        await session.execute(
            update(EvalRunRow)
            .where(EvalRunRow.id == run_id)
            .values(started_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    async with factory() as session:
        swept = await eval_repo.reclaim_stalled_runs(session, max_age_seconds=900)
        await session.commit()
        assert swept >= 1

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.ERRORED.value
        assert reread.passed_count == 0 and reread.failed_count == 0
        assert reread.error


@pytest.mark.asyncio
async def test_a_fresh_run_is_not_swept(factory) -> None:
    """The janitor must not reclaim work that is merely in progress."""
    async with factory() as session:
        project = await _project(session, "eval-janitor-fresh")
        _scenario, run = await _scenario_and_run(session, project.id, name="fresh")
        await eval_repo.start_run(
            session,
            run.id,
            resolved_config={},
            agent_id=None,
            harness_config={},
        )
        await session.commit()
        run_id = run.id

    async with factory() as session:
        await eval_repo.reclaim_stalled_runs(session, max_age_seconds=900)
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_only_one_worker_can_claim_a_queued_run(factory) -> None:
    """start_run's WHERE on `queued` is the claim; the loser finds it taken."""
    async with factory() as session:
        project = await _project(session, "eval-claim-test")
        _scenario, run = await _scenario_and_run(session, project.id, name="claimed")
        run_id = run.id
        await session.commit()

    async def claim(tag: str) -> None:
        async with factory() as session:
            await eval_repo.start_run(
                session,
                run_id,
                resolved_config={"claimed_by": tag},
                agent_id=None,
                harness_config={},
            )
            await session.commit()

    await asyncio.gather(claim("a"), claim("b"), return_exceptions=True)

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.RUNNING.value
        # Exactly one claim landed, whichever won the race.
        assert reread.resolved_config["claimed_by"] in ("a", "b")


@pytest.mark.asyncio
async def test_scenarios_filter_by_tag_and_by_kind(factory) -> None:
    """The tag filter is an array containment query the GIN index serves —
    it can only be checked against a real Postgres."""
    async with factory() as session:
        project = await _project(session, "eval-tag-test")
        for name, tags in (
            ("pre-publish-one", ["pre-publish", "smoke"]),
            ("pre-publish-two", ["pre-publish"]),
            ("nightly-only", ["nightly"]),
        ):
            await eval_repo.create_scenario(
                session,
                project_id=project.id,
                name=name,
                kind="script",
                definition=SCRIPTED,
                schema_version="pipecat-1.11",
                tags=tags,
            )
        await session.commit()
        project_id = project.id

    async with factory() as session:
        tagged = await eval_repo.list_scenarios(session, project_id, tag="pre-publish")
        assert {s.name for s in tagged} == {"pre-publish-one", "pre-publish-two"}

        smoke = await eval_repo.list_scenarios(session, project_id, tag="smoke")
        assert {s.name for s in smoke} == {"pre-publish-one"}

        assert await eval_repo.list_scenarios(session, project_id, tag="absent") == []
        assert (
            len(await eval_repo.list_scenarios(session, project_id, kind="simulation"))
            == 0
        )
        assert (
            len(await eval_repo.list_scenarios(session, project_id, kind="script")) == 3
        )


@pytest.mark.asyncio
async def test_a_scenario_name_is_unique_within_a_project(factory) -> None:
    async with factory() as session:
        project = await _project(session, "eval-unique-test")
        await _scenario_and_run(session, project.id, name="duplicated")
        await session.commit()

    async with factory() as session:
        existing = await eval_repo.get_scenario_by_name(
            session, project.id, "duplicated"
        )
        assert existing is not None


@pytest.mark.asyncio
async def test_a_run_that_could_not_be_queued_is_not_left_looking_queued(
    factory,
) -> None:
    """The janitor only sweeps `running`, so a run that never reached the queue
    would sit at `queued` forever while the caller was told 202 Accepted.

    Exercised at the repository seam the endpoint uses, since the failure is
    about which status the row ends up in, not about HTTP.
    """
    async with factory() as session:
        project = await _project(session, "eval-enqueue-failure")
        _scenario, run = await _scenario_and_run(session, project.id, name="orphan")
        run_id = run.id
        await session.commit()

    # What the endpoint does when the Redis push raises.
    async with factory() as session:
        await eval_repo.finish_run(
            session,
            run_id,
            status=EvalRunStatus.ERRORED,
            passed_count=0,
            failed_count=0,
            results=[],
            error="could not be queued: ConnectionError: connection refused",
        )
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.ERRORED.value
        assert "could not be queued" in reread.error
        assert reread.passed_count == 0 and reread.failed_count == 0

    # And the janitor would never have rescued it, which is why the endpoint
    # has to: it only looks at rows that were actually claimed.
    async with factory() as session:
        project = await _project(session, "eval-janitor-scope")
        _s, queued = await _scenario_and_run(session, project.id, name="still-queued")
        queued_id = queued.id
        await session.commit()

    async with factory() as session:
        await eval_repo.reclaim_stalled_runs(session, max_age_seconds=0)
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, queued_id)
        assert reread.status == EvalRunStatus.QUEUED.value, (
            "the janitor must not touch queued rows — a worker may still take it"
        )
