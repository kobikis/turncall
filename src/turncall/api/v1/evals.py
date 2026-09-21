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
    ScenarioDraftResponse,
    ScenarioFromCallRequest,
    ScenarioFromSessionRequest,
    UpdateEvalScenarioRequest,
)
from turncall.auth import Auth, WriteAuth
from turncall.config import get_settings
from turncall.domain.enums import EvalKind, EvalRunStatus, EvalToolPolicy
from turncall.evals import queue as eval_queue
from turncall.evals.runner import batch_outcome, resolved_scenario_snapshot
from turncall.evals.scenario import ScenarioError, validate
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


async def _record_runs(
    body: CreateEvalRunRequest,
    auth: Any,
    session: DbSession,
    scenarios: list[Any],
    batch_id: UUID,
) -> list[Any]:
    """One row per scenario, all carrying the batch id, none queued yet."""
    target = body.target.model_dump(mode="json", exclude_none=True)
    created = []
    for scenario in scenarios:
        created.append(
            await eval_repo.create_run(
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
        )
    return created


async def _scenarios_to_run(
    body: CreateEvalRunRequest, auth: Any, session: DbSession, settings: Any
) -> list[Any]:
    """The scenarios one run request covers: inline, a tag's fan-out, or one id.

    Exactly one of the three, enforced by the request schema. Split out of
    `create_eval_run` because selecting *what* to run and queueing it are two
    jobs, and the tag branch is the one with rules of its own.
    """
    if body.scenario is not None:
        # Supplied inline (#77), not stored: nothing is written to
        # `eval_scenarios`, the run's `scenario_id` stays null, and the
        # snapshot is the record. Same shape as an inline agent (ADR-0017).
        return [
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
    if body.tag:
        return await _scenarios_for_tag(body, auth, session, settings)

    scenario = await eval_repo.get_scenario(
        session, body.scenario_id, project_id=auth.project_id
    )
    if scenario is None:
        raise NotFoundError("EvalScenario", str(body.scenario_id))
    return [scenario]


async def _scenarios_for_tag(
    body: CreateEvalRunRequest, auth: Any, session: DbSession, settings: Any
) -> list[Any]:
    """Every scenario carrying the tag — or a refusal, never a subset (#97)."""
    scenarios = await eval_repo.list_scenarios(session, auth.project_id, tag=body.tag)
    if not scenarios:
        # An empty batch is worse than a rejection: it reports "0 failures"
        # forever, which reads as a pass. A typo'd tag is the common case.
        raise BadRequestError(f"no scenarios carry the tag {body.tag!r}")
    cap = settings.evals.max_scenarios_per_request
    if len(scenarios) > cap:
        # Refused, not truncated, for the same reason the empty tag is refused:
        # a batch that ran 50 of 200 scenarios reports a verdict for a suite
        # that never ran. `iterations` bounds the other axis; this is the one a
        # popular tag blows through.
        raise BadRequestError(
            f"the tag {body.tag!r} matches {len(scenarios)} scenarios, over the "
            f"limit of {cap} for one request "
            f"({len(scenarios) * body.iterations} conversations at "
            f"{body.iterations} iterations) — narrow the tag, or raise "
            f"EVAL_MAX_SCENARIOS_PER_REQUEST"
        )
    return scenarios


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

    scenarios = await _scenarios_to_run(body, auth, session, settings)

    batch_id = uuid4()
    created = await _record_runs(body, auth, session, scenarios, batch_id)
    await session.commit()

    # Committed before the push, so a run is never executed without being
    # recorded, and queued one at a time so one failed push does not cost the
    # others theirs.
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


@router.post("/from-call", status_code=201)
async def scenario_from_call(
    body: ScenarioFromCallRequest, auth: WriteAuth, session: DbSession
) -> dict:
    """Convert a completed call into a scripted scenario draft (#78).

    Returned for review by default; `save: true` stores it. Either way the
    tool mocks are seeded with what those tools actually returned, so the draft
    is safe to run under `mock_only` without repeating the call's side effects.
    """
    from turncall.domain.enums import CallEventType, CallStatus
    from turncall.services import scenario_from_call as convert
    from turncall.services.call_analysis_trigger import config_for_call
    from turncall.storage.repositories import call_repo, tool_invocation_repo

    call = await call_repo.get_call_by_id(
        session, body.call_id, project_id=auth.project_id
    )
    if call is None:
        raise NotFoundError("Call", str(body.call_id))
    if call.status not in (CallStatus.COMPLETED.value, CallStatus.FAILED.value):
        # A call still in progress has a transcript that will grow: a scenario
        # built from half a conversation looks complete and is not.
        raise BadRequestError(
            f"call {body.call_id} is {call.status!r} — convert it once it has ended"
        )

    transcript = await call_repo.list_call_events(
        session,
        body.call_id,
        event_type=CallEventType.TRANSCRIPT_FINAL,
        limit=1000,
    )
    invocations = await tool_invocation_repo.list_invocations_for_call(
        session, body.call_id
    )

    name = body.name or f"call-{str(body.call_id)[:8]}"
    try:
        draft = convert.build_scenario(
            transcript=convert.utterances_from_call_events(transcript),
            invocations=invocations,
            name=name,
        )
    except convert.ConversionError as exc:
        raise BadRequestError(str(exc)) from exc

    # Validated by the same round-trip a hand-written scenario gets: a draft
    # that cannot be parsed is not a draft, it is a bug report.
    try:
        validate(draft["definition"], name=name)
    except ScenarioError as exc:
        raise BadRequestError(f"the derived scenario does not parse: {exc}") from exc

    config = await config_for_call(session, call)
    target = convert.default_target(call, config)

    saved_id = None
    if body.save:
        from turncall.evals.scenario import kind_of

        row = await eval_repo.create_scenario(
            session,
            project_id=auth.project_id,
            name=name,
            kind=kind_of(draft["definition"]).value,
            definition=draft["definition"],
            schema_version=SCHEMA_VERSION,
            description=f"Derived from call {body.call_id}",
            tool_mocks=draft["tool_mocks"],
            tool_policy=EvalToolPolicy.MOCK_ONLY.value,
            tags=body.tags,
            default_target=target,
        )
        await session.commit()
        saved_id = row.id

    return ok(
        ScenarioDraftResponse(
            name=name,
            definition=draft["definition"],
            tool_mocks=draft["tool_mocks"],
            tool_policy=EvalToolPolicy.MOCK_ONLY,
            default_target=target,
            schema_version=SCHEMA_VERSION,
            note=convert.summarise(draft),
            saved=body.save,
            scenario_id=saved_id,
        )
    )


@router.post("/from-session", status_code=201)
async def scenario_from_session(
    body: ScenarioFromSessionRequest, auth: WriteAuth, session: DbSession
) -> dict:
    """Convert a finished text conversation into a scripted scenario draft (#87).

    The voice equivalent is `/from-call`. Both produce the same draft and both
    default to review rather than save, because a derived scenario asserts
    whatever the agent did that day — mistakes included.
    """
    from turncall.services import scenario_from_call as convert
    from turncall.storage.repositories import (
        sms_message_repo,
        sms_session_repo,
        tool_invocation_repo,
    )

    row = await sms_session_repo.get_session_by_id(
        session, body.session_id, project_id=auth.project_id
    )
    if row is None:
        raise NotFoundError("Session", str(body.session_id))

    messages = await sms_message_repo.list_messages_for_session(
        session, body.session_id, limit=1000
    )
    invocations = await tool_invocation_repo.list_invocations_for_session(
        session, body.session_id
    )

    name = body.name or f"session-{str(body.session_id)[:8]}"
    try:
        draft = convert.build_scenario(
            transcript=convert.utterances_from_session_messages(messages),
            invocations=invocations,
            name=name,
        )
    except convert.ConversionError as exc:
        raise BadRequestError(str(exc)) from exc

    try:
        validate(draft["definition"], name=name)
    except ScenarioError as exc:
        raise BadRequestError(f"the derived scenario does not parse: {exc}") from exc

    # A session names its agent directly — there is no call record and no
    # inline-config case to read around.
    target = {"type": "agent", "agent_id": str(row.agent_id)} if row.agent_id else None

    saved_id = None
    if body.save:
        from turncall.evals.scenario import kind_of

        stored = await eval_repo.create_scenario(
            session,
            project_id=auth.project_id,
            name=name,
            kind=kind_of(draft["definition"]).value,
            definition=draft["definition"],
            schema_version=SCHEMA_VERSION,
            description=f"Derived from session {body.session_id}",
            tool_mocks=draft["tool_mocks"],
            tool_policy=EvalToolPolicy.MOCK_ONLY.value,
            tags=body.tags,
            default_target=target,
        )
        await session.commit()
        saved_id = stored.id

    return ok(
        ScenarioDraftResponse(
            name=name,
            definition=draft["definition"],
            tool_mocks=draft["tool_mocks"],
            tool_policy=EvalToolPolicy.MOCK_ONLY,
            default_target=target,
            schema_version=SCHEMA_VERSION,
            note=convert.summarise(draft),
            saved=body.save,
            scenario_id=saved_id,
        )
    )


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
