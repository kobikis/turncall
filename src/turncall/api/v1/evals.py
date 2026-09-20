"""Eval scenario and run endpoints (#68, ADR-0018).

Scenarios are saved tests; runs execute one against a target over N iterations.
A run is accepted with 202 and executed by `turncall-eval-worker` — never in
this process (ADR-0004: event-loop jitter here is dead air on a live call).
"""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import BadRequestError, ConflictError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.evals import (
    SCHEMA_VERSION,
    CreateEvalRunRequest,
    CreateEvalScenarioRequest,
    EvalBatchResponse,
    EvalRunResponse,
    EvalScenarioResponse,
    UpdateEvalScenarioRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config import get_settings
from turncall.domain.enums import EvalKind, EvalRunStatus, EvalToolPolicy
from turncall.evals import queue as eval_queue
from turncall.evals.runner import batch_outcome, resolved_scenario_snapshot
from turncall.storage.repositories import eval_repo

router = APIRouter(prefix="/eval-scenarios", tags=["evals"])
runs_router = APIRouter(prefix="/eval-runs", tags=["evals"])


@router.post("", status_code=201)
async def create_eval_scenario(
    body: CreateEvalScenarioRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create a scenario. The definition is validated by round-tripping it
    through pipecat's parser and then stored verbatim."""
    existing = await eval_repo.get_scenario_by_name(session, auth.project_id, body.name)
    if existing is not None:
        raise ConflictError(f"Eval scenario {body.name!r} already exists")
    row = await eval_repo.create_scenario(
        session,
        project_id=auth.project_id,
        name=body.name,
        kind=body.kind.value,
        definition=body.definition,
        schema_version=SCHEMA_VERSION,
        description=body.description,
        tool_mocks=body.tool_mocks,
        # One place decides the fail-closed default, for the boundary and the
        # worker alike: `live` has to be typed.
        tool_policy=EvalToolPolicy.resolve(body.tool_policy).value,
        tags=body.tags,
        default_target=body.default_target,
    )
    await session.commit()
    return ok(EvalScenarioResponse.model_validate(row))


@router.get("")
async def list_eval_scenarios(
    auth: Auth,
    session: DbSession,
    kind: EvalKind | None = None,
    tag: str | None = None,
) -> dict:
    rows = await eval_repo.list_scenarios(
        session, auth.project_id, kind=kind.value if kind else None, tag=tag
    )
    return ok([EvalScenarioResponse.model_validate(r) for r in rows])


@router.get("/{scenario_id}")
async def get_eval_scenario(scenario_id: UUID, auth: Auth, session: DbSession) -> dict:
    row = await eval_repo.get_scenario(session, scenario_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("EvalScenario", str(scenario_id))
    return ok(EvalScenarioResponse.model_validate(row))


@router.put("/{scenario_id}")
async def update_eval_scenario(
    scenario_id: UUID,
    body: UpdateEvalScenarioRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    row = await eval_repo.get_scenario(session, scenario_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("EvalScenario", str(scenario_id))

    values: dict = {
        k: v
        for k, v in {
            "name": body.name,
            "description": body.description,
            "definition": body.definition,
            "tool_mocks": body.tool_mocks,
            "tool_policy": body.tool_policy.value if body.tool_policy else None,
            "tags": body.tags,
            "default_target": body.default_target,
        }.items()
        if v is not None
    }
    if body.definition is not None:
        # A new definition may change the kind, and the column is what readers
        # switch on — leaving it stale would make the row lie about itself.
        from turncall.evals.scenario import kind_of

        values["kind"] = kind_of(body.definition).value
        values["schema_version"] = SCHEMA_VERSION

    updated = row
    if values:
        updated = await eval_repo.update_scenario(session, scenario_id, values=values)
        await session.commit()
    return ok(EvalScenarioResponse.model_validate(updated))


@router.delete("/{scenario_id}")
async def delete_eval_scenario(
    scenario_id: UUID, auth: WriteAuth, session: DbSession
) -> dict:
    """Delete a scenario. Its runs survive, with the scenario name intact."""
    row = await eval_repo.get_scenario(session, scenario_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("EvalScenario", str(scenario_id))
    await eval_repo.delete_scenario(session, scenario_id)
    await session.commit()
    return ok({"deleted": True})


async def _queue_one(session: DbSession, run: Any) -> str:
    """Push one recorded run onto the worker queue, reporting what holds.

    The row is committed before the push, so a run is never executed without
    being recorded. If the push then fails there is nothing to execute it — the
    janitor only sweeps `running`, so the row would sit at `queued` forever
    while the caller was told it was accepted.
    """
    try:
        from turncall.storage.redis import get_redis

        await eval_queue.enqueue(get_redis(), run.id)
        return run.status
    except Exception as exc:
        logger.exception("eval_enqueue_failed", run_id=str(run.id))
        # Guarded: a push can raise after the write landed (the reply read times
        # out), and a worker may already have claimed the run. Stomping a run
        # that is legitimately in flight would report a failure that never
        # happened, so only a still-queued row is failed here.
        failed = await eval_repo.fail_if_still_queued(
            session,
            run.id,
            error=f"could not be queued: {type(exc).__name__}: {exc}",
        )
        await session.commit()
        return EvalRunStatus.ERRORED.value if failed else run.status


@runs_router.post("", status_code=202)
async def create_eval_run(
    body: CreateEvalRunRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Queue a run per scenario. 202: the worker executes them, never this
    process.

    A `tag` fans out to every scenario carrying it; the runs share one batch id
    so a single request has a single readable verdict. Each run is queued
    independently, so one that cannot be pushed does not cost the others theirs.
    """
    settings = get_settings()
    if body.iterations > settings.evals.max_iterations:
        raise BadRequestError(
            f"iterations exceeds the limit of {settings.evals.max_iterations}"
        )

    if body.scenario is not None:
        # Supplied inline (#77), not stored: nothing is written to
        # `eval_scenarios`, the run's `scenario_id` stays null, and the
        # snapshot is the record. Same shape as an inline agent (ADR-0017).
        scenarios = [
            SimpleNamespace(
                id=None,
                name=body.scenario.name,
                kind=body.scenario.kind.value,
                definition=body.scenario.definition,
                schema_version=SCHEMA_VERSION,
                tool_mocks=body.scenario.tool_mocks or {},
                tool_policy=(
                    body.scenario.tool_policy or EvalToolPolicy.MOCK_ONLY
                ).value,
            )
        ]
    elif body.tag:
        scenarios = await eval_repo.list_scenarios(
            session, auth.project_id, tag=body.tag
        )
        if not scenarios:
            # An empty batch is worse than a rejection: it reports "0 failures"
            # forever, which reads as a pass. A typo'd tag is the common case.
            raise BadRequestError(f"no scenarios carry the tag {body.tag!r}")
    else:
        scenario = await eval_repo.get_scenario(
            session, body.scenario_id, project_id=auth.project_id
        )
        if scenario is None:
            raise NotFoundError("EvalScenario", str(body.scenario_id))
        scenarios = [scenario]

    batch_id = uuid4()
    target = body.target.model_dump(mode="json", exclude_none=True)
    created = []
    for scenario in scenarios:
        run = await eval_repo.create_run(
            session,
            project_id=auth.project_id,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            kind=scenario.kind,
            target=target,
            resolved_scenario=resolved_scenario_snapshot(
                definition=scenario.definition,
                schema_version=scenario.schema_version,
                tool_mocks=scenario.tool_mocks,
                tool_policy=scenario.tool_policy,
            ),
            modality=body.modality.value,
            iterations=body.iterations,
            batch_id=batch_id,
        )
        created.append(run)
    await session.commit()

    runs = [
        {
            "id": str(run.id),
            "scenario_id": str(run.scenario_id) if run.scenario_id else None,
            "scenario_name": run.scenario_name,
            "status": await _queue_one(session, run),
        }
        for run in created
    ]
    return ok({"batch_id": str(batch_id), "runs": runs})


@runs_router.get("/batches/{batch_id}")
async def get_eval_batch(batch_id: UUID, auth: Auth, session: DbSession) -> dict:
    """A batch's outcome without fetching every run's transcripts (#75).

    Route declared before `/{run_id}` so a literal path segment is not read as
    a run id — FastAPI matches in declaration order.
    """
    rows = await eval_repo.list_runs(session, auth.project_id, batch_id=batch_id)
    if not rows:
        raise NotFoundError("EvalBatch", str(batch_id))
    return ok(EvalBatchResponse.model_validate(batch_outcome(batch_id, rows)))


@runs_router.get("")
async def list_eval_runs(
    auth: Auth,
    session: DbSession,
    batch_id: UUID | None = None,
    scenario_id: UUID | None = None,
    status: EvalRunStatus | None = None,
) -> dict:
    rows = await eval_repo.list_runs(
        session,
        auth.project_id,
        batch_id=batch_id,
        scenario_id=scenario_id,
        status=status.value if status else None,
    )
    return ok([EvalRunResponse.model_validate(r) for r in rows])


@runs_router.get("/{run_id}")
async def get_eval_run(run_id: UUID, auth: Auth, session: DbSession) -> dict:
    row = await eval_repo.get_run(session, run_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("EvalRun", str(run_id))
    return ok(EvalRunResponse.model_validate(row))


@runs_router.delete("/{run_id}")
async def cancel_eval_run(run_id: UUID, auth: WriteAuth, session: DbSession) -> dict:
    """Cancel a run while it is queued or running."""
    row = await eval_repo.get_run(session, run_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("EvalRun", str(run_id))
    cancelled = await eval_repo.cancel_run(session, run_id)
    await session.commit()
    if not cancelled:
        raise ConflictError(f"Run already finished with status {row.status!r}")
    return ok({"cancelled": True})
