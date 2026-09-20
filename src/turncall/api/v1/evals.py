"""Eval scenario and run endpoints (#68, ADR-0018).

Scenarios are saved tests; runs execute one against a target over N iterations.
A run is accepted with 202 and executed by `turncall-eval-worker` — never in
this process (ADR-0004: event-loop jitter here is dead air on a live call).
"""

from uuid import UUID, uuid4

from fastapi import APIRouter
from loguru import logger

from turncall.api.deps import DbSession
from turncall.api.errors import ConflictError, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.evals import (
    SCHEMA_VERSION,
    CreateEvalRunRequest,
    CreateEvalScenarioRequest,
    EvalRunResponse,
    EvalScenarioResponse,
    UpdateEvalScenarioRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config import get_settings
from turncall.evals import queue as eval_queue
from turncall.evals.runner import resolved_scenario_snapshot
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
        tool_policy=body.tool_policy.value,
        tags=body.tags,
        default_target=body.default_target,
    )
    await session.commit()
    return ok(EvalScenarioResponse.model_validate(row))


@router.get("")
async def list_eval_scenarios(
    auth: Auth,
    session: DbSession,
    kind: str | None = None,
    tag: str | None = None,
) -> dict:
    rows = await eval_repo.list_scenarios(session, auth.project_id, kind=kind, tag=tag)
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
            "tags": body.tags,
            "default_target": body.default_target,
        }.items()
        if v is not None
    }
    if body.tool_policy is not None:
        values["tool_policy"] = body.tool_policy.value
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


@runs_router.post("", status_code=202)
async def create_eval_run(
    body: CreateEvalRunRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Queue a run. 202: the worker executes it, this process never does."""
    settings = get_settings()
    if body.iterations > settings.evals.max_iterations:
        raise ConflictError(
            f"iterations exceeds the limit of {settings.evals.max_iterations}"
        )

    scenario = await eval_repo.get_scenario(
        session, body.scenario_id, project_id=auth.project_id
    )
    if scenario is None:
        raise NotFoundError("EvalScenario", str(body.scenario_id))

    batch_id = uuid4()
    run = await eval_repo.create_run(
        session,
        project_id=auth.project_id,
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        kind=scenario.kind,
        target=body.target.model_dump(mode="json", exclude_none=True),
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
    await session.commit()

    # The row is committed before the queue push, so a Redis outage leaves a
    # queued run the janitor's operator can see and requeue -- not a run that
    # was executed but never recorded.
    try:
        from turncall.storage.redis import get_redis

        await eval_queue.enqueue(get_redis(), run.id)
    except Exception:
        logger.exception("eval_enqueue_failed", run_id=str(run.id))

    return ok(
        {
            "batch_id": str(batch_id),
            "runs": [
                {
                    "id": str(run.id),
                    "scenario_name": run.scenario_name,
                    "status": run.status,
                }
            ],
        }
    )


@runs_router.get("")
async def list_eval_runs(
    auth: Auth,
    session: DbSession,
    batch_id: UUID | None = None,
    scenario_id: UUID | None = None,
    status: str | None = None,
) -> dict:
    rows = await eval_repo.list_runs(
        session,
        auth.project_id,
        batch_id=batch_id,
        scenario_id=scenario_id,
        status=status,
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
