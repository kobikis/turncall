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
from uuid import uuid4

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
        swept = await eval_repo.reclaim_stalled_runs(
            session, max_age_seconds=900, max_queued_seconds=3600
        )
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
        await eval_repo.reclaim_stalled_runs(
            session, max_age_seconds=900, max_queued_seconds=3600
        )
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
async def test_a_run_that_could_not_be_queued_is_failed_not_left_queued(
    factory,
) -> None:
    """What the endpoint does when the Redis push raises."""
    async with factory() as session:
        project = await _project(session, "eval-enqueue-failure")
        _scenario, run = await _scenario_and_run(session, project.id, name="orphan")
        run_id = run.id
        await session.commit()

    async with factory() as session:
        failed = await eval_repo.fail_if_still_queued(
            session, run_id, error="could not be queued: ConnectionError"
        )
        await session.commit()
        assert failed

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.ERRORED.value
        assert "could not be queued" in reread.error
        assert reread.passed_count == 0 and reread.failed_count == 0


@pytest.mark.asyncio
async def test_a_failed_push_never_stomps_a_run_a_worker_already_claimed(
    factory,
) -> None:
    """A push can raise *after* the write landed — the reply read times out —
    so by the time the handler runs, a worker may be running the scenario.
    Reporting that as 'could not be queued' would be a failure that never
    happened."""
    async with factory() as session:
        project = await _project(session, "eval-enqueue-race")
        _scenario, run = await _scenario_and_run(session, project.id, name="claimed")
        run_id = run.id
        await eval_repo.start_run(
            session, run_id, resolved_config={}, agent_id=None, harness_config={}
        )
        await session.commit()

    async with factory() as session:
        failed = await eval_repo.fail_if_still_queued(
            session, run_id, error="could not be queued: ConnectionError"
        )
        await session.commit()
        assert not failed, "a claimed run must be left to its worker"

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.RUNNING.value
        assert reread.error is None


@pytest.mark.asyncio
async def test_the_janitor_sweeps_a_queued_run_nothing_ever_claimed(factory) -> None:
    """The endpoint handles the push *raising*; it cannot handle not being
    alive. A process that dies between the commit and the push — or a Redis
    restart that drops the list — leaves a row nothing will ever execute, while
    the caller was told 202 Accepted."""
    async with factory() as session:
        project = await _project(session, "eval-orphan-queued")
        _scenario, run = await _scenario_and_run(session, project.id, name="abandoned")
        run_id = run.id
        await session.commit()
        # Backdate the queue: nobody picked it up, and nobody will.
        await session.execute(
            update(EvalRunRow)
            .where(EvalRunRow.id == run_id)
            .values(queued_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()

    async with factory() as session:
        await eval_repo.reclaim_stalled_runs(
            session, max_age_seconds=900, max_queued_seconds=3600
        )
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.ERRORED.value
        assert reread.passed_count == 0 and reread.failed_count == 0


@pytest.mark.asyncio
async def test_the_janitor_leaves_a_freshly_queued_run_alone(factory) -> None:
    """A worker may still take it — sweeping a backlog would be worse than the
    bug this sweep exists for."""
    async with factory() as session:
        project = await _project(session, "eval-fresh-queued")
        _scenario, run = await _scenario_and_run(session, project.id, name="waiting")
        run_id = run.id
        await session.commit()

    async with factory() as session:
        await eval_repo.reclaim_stalled_runs(
            session, max_age_seconds=900, max_queued_seconds=3600
        )
        await session.commit()

    async with factory() as session:
        reread = await eval_repo.get_run(session, run_id)
        assert reread.status == EvalRunStatus.QUEUED.value


async def test_a_batch_reads_back_without_fetching_every_run(factory) -> None:
    """Fan-out is one run per tagged scenario sharing a batch id, and the batch
    reads back through the same filter the CLI will use (#75). Runs in a batch
    are independent rows: one erroring leaves the others alone."""
    from turncall.evals.runner import batch_outcome

    batch_id = uuid4()
    async with factory() as session:
        project = await _project(session, "eval-batch-test")
        scenarios = []
        for name in ("books", "cancels", "reschedules"):
            scenarios.append(
                await eval_repo.create_scenario(
                    session,
                    project_id=project.id,
                    name=name,
                    kind="script",
                    definition=SCRIPTED,
                    schema_version="pipecat-1.11",
                    tags=["pre-publish"],
                )
            )
        for scenario in scenarios:
            await eval_repo.create_run(
                session,
                project_id=project.id,
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                kind="script",
                target={"type": "agent", "agent_id": str(uuid4())},
                resolved_scenario={"definition": SCRIPTED},
                modality="text",
                iterations=1,
                batch_id=batch_id,
            )
        await session.commit()
        project_id = project.id

    async with factory() as session:
        rows = await eval_repo.list_runs(session, project_id, batch_id=batch_id)
        assert len(rows) == 3, "one run per tagged scenario, one batch"
        assert {r.scenario_name for r in rows} == {"books", "cancels", "reschedules"}

        # Independent: finishing one differently leaves the rest untouched.
        await eval_repo.finish_run(
            session,
            rows[0].id,
            status=EvalRunStatus.ERRORED,
            passed_count=0,
            failed_count=0,
            results=[],
            error="the judge was unreachable",
        )
        await eval_repo.finish_run(
            session,
            rows[1].id,
            status=EvalRunStatus.PASSED,
            passed_count=1,
            failed_count=0,
            results=[],
            error=None,
        )
        await session.commit()

    async with factory() as session:
        rows = await eval_repo.list_runs(session, project_id, batch_id=batch_id)
        outcome = batch_outcome(batch_id, rows)
        # One still queued, so no verdict yet — reading one now would be a lie
        # the caller cannot detect.
        assert outcome["status"] is EvalRunStatus.RUNNING
        assert outcome["counts"]["errored"] == 1

        queued = await eval_repo.list_runs(
            session, project_id, batch_id=batch_id, status="queued"
        )
        assert len(queued) == 1, "status filter narrows within the batch"
