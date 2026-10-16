"""Running one eval run: plan it, iterate, finish it.

This module is the seam the worker calls and the unit tests drive. Everything
that needs a pipeline and a socket lives behind the injected `execute` callable
(`turncall.evals.harness.run_iteration` in production), so the loop, the
snapshots and the iteration budgets are testable with canned pipecat result
objects.

Four siblings hold the parts that are pure functions rather than orchestration,
all imported here and none importing this module back: `targets` resolves what
the run points at, `result_mapping` turns one pipecat result into one `results`
entry (and `derive_status` the run's verdict out of them), `run_warnings`
builds what the run has to tell its author that is not a verdict, and
`snapshots` builds the three records of what actually ran.

Two rules from the design are load-bearing here:

- `errored` is not a kind of `failed`. An iteration whose harness did not
  complete — a connect failure, a judge outage — is neither a pass nor a fail
  and never counts toward a rate.
- Three snapshots are written before the first iteration runs: the agent config
  that actually ran, the scenario as it stood, and the harness config. A result
  is uninterpretable without all three, and all three can change afterwards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from loguru import logger

from turncall.domain.config_secrets import sanitize_config, sanitize_target
from turncall.domain.enums import (
    EvalKind,
    EvalModality,
    EvalRunStatus,
    EvalToolPolicy,
)
from turncall.evals import scenario as scenario_mod
from turncall.evals.build import worker_build
from turncall.evals.result_mapping import (
    IterationOutcome,
    derive_status,
    errored_entry,
    map_script_result,
    map_simulation_result,
)
from turncall.evals.run_warnings import (
    warn_ambience_skipped,
    warn_context_ignored,
    warn_judge_changed,
    warn_unmatched_mocks,
    warn_worker_stale,
)
from turncall.evals.snapshots import harness_config
from turncall.evals.targets import ResolvedTarget, resolve_target
from turncall.services.credentials import Credentials, for_project
from turncall.services.credentials import scope as credentials_scope
from turncall.services.tool_mocks import ToolMocks

ExecuteIteration = Callable[..., Awaitable[Any]]


# The floor under a derived per-iteration budget (#93). The whole-run budget
# split 50 ways is 18 seconds, which one audio simulation exceeds by design —
# a budget that tight would abandon every iteration of a run nobody
# misconfigured. #94 derives the janitor's cutoff from this same number, so a
# run allowed to take longer than `max_run_duration_seconds` is not then swept
# for taking it.
MIN_ITERATION_BUDGET_S = 180.0


# What the janitor allows a run *on top of* its iterations' budgets before
# calling it abandoned (#94): the last iteration still has a bot pipeline to
# wind down (up to `_BOT_STOP_TIMEOUT_S`), a verdict to write and an event to
# dispatch. Flat, because none of that scales with the iteration count.
RECLAIM_MARGIN_S = 60.0


def iteration_budget_seconds(settings: Any, iterations: int) -> float:
    """How long one iteration may run before it is abandoned (#93).

    Derived from the run budget and the work the run was asked to do, rather
    than being a constant of its own: one number should govern how long a run
    may take. The floor is what keeps a high iteration count from turning that
    division into a budget no real conversation fits in.
    """
    whole = getattr(getattr(settings, "evals", None), "max_run_duration_seconds", 0)
    share = float(whole or 0) / max(iterations, 1)
    return max(share, MIN_ITERATION_BUDGET_S)


def run_budget_seconds(settings: Any, iterations: int) -> float:
    """How long a whole run may take before nothing but a crash explains it.

    #93's per-iteration bound is what makes this knowable: iterations are
    serial, so the run's real ceiling is the sum of theirs plus the margin.
    The janitor compares against this rather than against
    `max_run_duration_seconds` flat, which had nothing to do with how much work
    the run was asked to do — a 10-iteration audio simulation passes 900s
    comfortably, and sweeping it mid-flight made `turncall eval run` exit 2 on
    a run that passed.

    The janitor cannot call this per row, so it rebuilds the same arithmetic in
    SQL from `MIN_ITERATION_BUDGET_S` and `RECLAIM_MARGIN_S`; the identity is
    pinned by `test_the_sql_cutoff_is_the_same_arithmetic`.
    """
    return (
        iterations * iteration_budget_seconds(settings, iterations) + RECLAIM_MARGIN_S
    )


def describe_failure(exc: BaseException, *, prefix_type: bool = True) -> str:
    """An error as the run records it.

    An ImportError in a worker older than its source is not a bug in the code
    it names: the process holds the old version of one module and has just
    loaded the new version of another. Said plainly, because the bare message
    sends whoever reads it looking for a missing name that is right there.
    """
    text = f"{type(exc).__name__}: {exc}" if prefix_type else str(exc)
    if isinstance(exc, ImportError) and worker_build()["worker_stale"]:
        text += (
            " — the eval worker started before its code last changed, so it is "
            "mixing old and new modules. Restart the eval worker and run again."
        )
    return text


@dataclass(frozen=True)
class IterationPlan:
    """Everything one iteration needs, resolved once before the first runs.

    All of it already travelled together into every `execute()` call; the mocks
    and the policy joined it in #71. Bundled so the loop takes one argument and
    `execute_run` keeps to setting the run up and writing the verdict down.
    """

    parsed: Any
    kind: EvalKind
    target: ResolvedTarget
    modality: EvalModality
    settings: Any
    session_factory: Any
    run_id: UUID
    tool_mocks: dict[str, Any]
    live_tools: bool
    timeout_s: float
    credentials: Credentials

    def fresh_mocks(self) -> ToolMocks:
        """A recorder for one iteration.

        The mocks are the scenario's, so every iteration gets the same ones —
        but what was called and what was refused belong to the iteration alone.
        """
        return ToolMocks(responses=self.tool_mocks, live=self.live_tools)

    def execute_kwargs(self) -> dict[str, Any]:
        """What `harness.run_iteration` is called with, mocks aside."""
        return {
            "parsed": self.parsed,
            "kind": self.kind,
            "target": self.target,
            "modality": self.modality,
            "settings": self.settings,
            "session_factory": self.session_factory,
            "run_id": self.run_id,
            "timeout_s": self.timeout_s,
        }


def tool_policy_of(resolved_scenario: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The run's mocks, and whether a tool without one may execute (#71).

    Read off the run's snapshot rather than the scenario row: the row can be
    edited while the run sits queued, and a run is what it was queued as.
    `EvalToolPolicy.resolve` is the one place the fail-closed default lives.
    """
    policy = EvalToolPolicy.resolve(resolved_scenario.get("tool_policy"))
    return dict(
        resolved_scenario.get("tool_mocks") or {}
    ), policy is EvalToolPolicy.LIVE


async def _was_cancelled(plan: IterationPlan) -> bool:
    """Has the run been cancelled since the last iteration?

    Read on its own short-lived session: the loop holds none, deliberately, so
    a run taking minutes does not sit on a pooled connection. A check that
    cannot be made is not a cancellation — a database hiccup must not abandon
    a run that nobody stopped.
    """
    from turncall.storage.repositories import eval_repo

    try:
        async with plan.session_factory() as session:
            run = await eval_repo.get_run(session, plan.run_id)
    except Exception as exc:
        # With the reason: a check that fails every iteration is otherwise a
        # repeated bare line, and this is the one branch that decides to keep
        # paying for a run nobody may still want.
        logger.warning(
            "eval_cancel_check_failed", run_id=str(plan.run_id), error=str(exc)
        )
        return False
    return run is not None and run.status == EvalRunStatus.CANCELLED.value


async def run_iterations(
    plan: IterationPlan, *, iterations: int, execute: ExecuteIteration
) -> list[IterationOutcome]:
    """Run the scenario `iterations` times and collect what each one proved.

    One bad iteration never stops the rest: a run of five that lost one to a
    judge timeout still has four verdicts, and that entry says what happened.
    """
    outcomes: list[IterationOutcome] = []
    for iteration in range(1, iterations + 1):
        if await _was_cancelled(plan):
            # Between iterations is the cheap seam (#92): it bounds the waste
            # at one iteration, where tearing down a live pipeline mid-turn
            # would buy a fraction of that for real complexity. Whatever ran
            # is returned; `finish_run` will decline to overwrite the row.
            logger.info(
                "eval_run_cancelled", run_id=str(plan.run_id), after=iteration - 1
            )
            break
        mocks = plan.fresh_mocks()
        agent_errors: list[str] = []
        try:
            result = await execute(
                **plan.execute_kwargs(), tool_mocks=mocks, agent_errors=agent_errors
            )
        except Exception as exc:
            logger.exception("eval_iteration_error", run_id=str(plan.run_id))
            outcomes.append(
                with_agent_errors(
                    errored_entry(
                        iteration, describe_failure(exc), tool_calls=mocks.calls
                    ),
                    agent_errors,
                )
            )
            continue
        if mocks.refused:
            # Fail closed (#71): the agent asked for a tool no mock covers, so
            # nothing ran and the iteration says nothing about the agent.
            # `errored`, not `failed` — the scenario was never finished. The
            # run stops here: the remaining iterations would hit the same
            # missing mock, and paying an LLM to prove it nine more times helps
            # nobody.
            outcomes.append(
                errored_entry(
                    iteration,
                    f"unmocked tool: {mocks.refused[0]}",
                    tool_calls=mocks.calls,
                    refused_tool=mocks.refused[0],
                )
            )
            break
        if plan.kind is EvalKind.SIMULATION:
            outcome = map_simulation_result(
                result, iteration=iteration, tool_calls=mocks.calls
            )
        else:
            outcome = map_script_result(
                result,
                iteration=iteration,
                parsed=plan.parsed,
                tool_calls=mocks.calls,
            )
        outcomes.append(with_agent_errors(outcome, agent_errors))
    return outcomes


def with_agent_errors(
    outcome: IterationOutcome, agent_errors: list[str]
) -> IterationOutcome:
    """Attach what the agent's pipeline reported to the iteration's entry.

    On an iteration that failed they also lead its `failures`, because they are
    usually the reason: a provider that refused every request makes the agent
    say nothing, and the assertion that notices only says "no response".
    """
    if not agent_errors:
        return outcome
    entry = {**outcome.entry, "agent_errors": list(agent_errors)}
    if outcome.passed is False:
        leading = [{"kind": "agent_error", "reason": e} for e in agent_errors]
        entry["failures"] = leading + list(entry.get("failures") or [])
    return replace(outcome, entry=entry)


def _completed_payload(run: Any) -> dict[str, Any]:
    """The whole result, read back off the row that was just written.

    Comprehensive on purpose (ADR-0006): one terminal event carrying status,
    counts, every iteration's transcript and failures, and the three snapshots
    — the same shape of promise `call.ended` makes. A subscriber that gets this
    never has to call back to find out what happened, which is the entire point
    of not shipping a scatter of partials.

    Built from the row rather than from the values in hand so the event cannot
    disagree with what was stored.
    """
    return {
        "status": run.status,
        "passed_count": run.passed_count,
        "failed_count": run.failed_count,
        "iterations": run.iterations,
        "error": run.error,
        "scenario_id": str(run.scenario_id) if run.scenario_id else None,
        "scenario_name": run.scenario_name,
        "kind": run.kind,
        "modality": run.modality,
        "batch_id": str(run.batch_id) if run.batch_id else None,
        "target": sanitize_target(run.target),
        # Null for an inline target — the honest answer to "which stored agent
        # was this", not missing data (ADR-0017).
        "agent_id": str(run.agent_id) if run.agent_id else None,
        "agent_version": run.agent_version,
        "results": run.results,
        # What the worker's log used to be the only home for (#96).
        "warnings": list(run.warnings or []),
        "resolved_config": sanitize_config(run.resolved_config),
        "resolved_scenario": run.resolved_scenario,
        "harness_config": run.harness_config,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


async def _dispatch_run_event(
    session: Any,
    *,
    event_type: Any,
    run_id: UUID,
    project_id: UUID,
    agent_id: UUID | None,
    payload: dict[str, Any],
) -> None:
    """Send one eval event to the project's webhook subscribers (#76).

    Best-effort by design, like every other dispatch site: a subscriber that is
    down must not turn a finished run into a failed one, and the row is already
    written by the time this runs. The run id rides in the **envelope**, not the
    payload — ADR-0007's rule, the same place `call_id` and `session_id` live —
    and `agent_id` is null for an inline target, which is the honest answer
    rather than missing data (ADR-0017).
    """
    from turncall.events.emit import dispatch_project_event

    try:
        await dispatch_project_event(
            session,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            eval_run_id=run_id,
            agent_id=agent_id,
        )
    except Exception:
        logger.exception("eval_event_dispatch_failed", run_id=str(run_id))


async def _record_unrunnable(session: Any, run: Any, exc: Exception) -> None:
    """A run that cannot start is `errored` before an iteration is paid for.

    It still dispatches `eval.run.completed` (#76). A run that was accepted and
    then vanished is worse than one that failed: a subscriber waiting on a
    terminal event would wait forever, and `errored` is a terminal status.
    """
    from turncall.domain.enums import CallEventType
    from turncall.storage.repositories import eval_repo

    written = await eval_repo.finish_run(
        session,
        run.id,
        status=EvalRunStatus.ERRORED,
        passed_count=0,
        failed_count=0,
        results=[],
        error=describe_failure(exc, prefix_type=False),
    )
    await session.commit()
    logger.warning("eval_run_unrunnable", run_id=str(run.id), error=str(exc))
    if not written:
        # Cancelled between the claim and the failure (#92). The row is already
        # terminal and is not this path's to relabel.
        return

    finished = await eval_repo.get_run(session, run.id)
    if finished is not None:
        await _dispatch_run_event(
            session,
            event_type=CallEventType.EVAL_RUN_COMPLETED,
            run_id=run.id,
            project_id=run.project_id,
            # Nothing resolved, so there is no agent to name.
            agent_id=None,
            payload=_completed_payload(finished),
        )


def _parse_for_run(
    run: Any, modality: EvalModality, settings: Any = None
) -> tuple[EvalKind, Any]:
    """The pipecat scenario this run will drive, and the kind it turned out to be.

    The definition decides, not `run.kind`. The column is a copy made at queue
    time; if the two ever disagree, the mapper is chosen from what actually
    parses, and `map_script_result` would otherwise be handed a simulation
    result to read.
    """
    definition = dict(run.resolved_scenario.get("definition") or {})
    kind = scenario_mod.kind_of(definition)
    # The scenario's own judge and persona, compiled into pipecat's blocks. A
    # raw block inside the definition wins — see `with_models`. Narrowest wins
    # the whole way down (#119): the scenario's block, then the platform
    # default, then pipecat's local Ollama, which is what `None` leaves.
    judge, simulator = _platform_models(settings)
    definition = scenario_mod.with_models(
        definition,
        judge=run.resolved_scenario.get("judge") or judge,
        simulator=run.resolved_scenario.get("simulator") or simulator,
        # Only read to decide the explainer (#139): a classifier judge has no
        # LLM of its own to write reasons with, and pipecat's fallback for one
        # is a local Ollama nobody configured.
        platform_judge=judge,
    )
    merged = scenario_mod.with_modality(definition, modality)
    return kind, scenario_mod.parse(merged, name=run.scenario_name)


def _platform_models(
    settings: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The platform's judge and simulator blocks, or a pair of Nones (#119).

    A run is never a place to set these. Letting a request swap the judge makes
    two runs of one scenario incomparable with nothing on either row saying
    why — the same reason mocks belong to the scenario rather than the run.
    """
    from turncall.evals.judges import default_block

    evals = getattr(settings, "evals", None)
    if evals is None:
        return None, None
    return (
        default_block(evals.judge_provider, evals.judge_model, evals.judge_temperature),
        default_block(
            evals.simulator_provider,
            evals.simulator_model,
            evals.simulator_temperature,
        ),
    )


# What makes two runs of one scenario comparable. A verdict is the judge's, so
# a baseline that moved because the judge moved is not a regression — and read
# off the row alone, the two are indistinguishable (#119).
async def _plan_run(
    session: Any, run: Any, *, settings: Any, session_factory: Any
) -> IterationPlan | None:
    """Resolve what this run will execute, and write its snapshots.

    Returns None when the run cannot start — the row is already recorded as
    `errored` by then. Everything here happens once, before the first
    iteration: the snapshots have to describe what actually ran, and a
    definition that stopped parsing must not be discovered per iteration.
    """
    from turncall.storage.repositories import eval_repo

    modality = EvalModality(run.modality)
    try:
        kind, parsed = _parse_for_run(run, modality, settings)
        target = await resolve_target(
            session, project_id=run.project_id, target=run.target
        )
        # Fails closed (ADR-0020): unreadable project keys error the run rather
        # than grading it on the platform's account, which nothing would show.
        credentials = await for_project(session, run.project_id)
    except Exception as exc:
        await _record_unrunnable(session, run, exc)
        return None

    tool_mocks, live_tools = tool_policy_of(run.resolved_scenario)
    # Carried into the run, not only the create response (#95): the person who
    # reads a green verdict is not always the one who wrote the scenario, and
    # "this could not have failed" is the most important thing a green run can
    # say about itself.
    harness = harness_config(parsed)
    warnings = scenario_mod.assertion_warnings(parsed)
    warnings += warn_unmatched_mocks(tool_mocks, target, run_id=run.id)
    warnings += await warn_judge_changed(session, run, harness)
    warnings += warn_worker_stale(harness)
    warnings += warn_ambience_skipped(target)
    warnings += warn_context_ignored(parsed, target)
    if live_tools and (target.config.tools or target.config.mcp_servers):
        # The scenario typed the word, so this is allowed — but a real webhook
        # fires on every iteration, and the run's record is where someone
        # reading the result will look for that (#96).
        logger.warning(
            "eval_agent_has_live_tools",
            run_id=str(run.id),
            agent_id=str(target.agent_id),
            tools=len(target.config.tools),
            mcp_servers=len(target.config.mcp_servers),
        )
        warnings.append(
            {
                "code": "live_tools_allowed",
                "message": (
                    f"tool_policy is 'live': the agent's {len(target.config.tools)} "
                    "tool(s) really execute, once per iteration"
                ),
                "tools": [t.name for t in target.config.tools],
                "mcp_servers": len(target.config.mcp_servers),
            }
        )

    await eval_repo.start_run(
        session,
        run.id,
        # Stored already masked (#91): nothing reads this back to execute
        # anything — it answers "what config produced this verdict", which a
        # masked key answers as well as a real one.
        resolved_config=sanitize_config(target.config_blob),
        agent_id=target.agent_id,
        agent_version=target.agent_version,
        harness_config=harness,
        warnings=warnings,
    )
    await session.commit()

    # After the commit, not before: an event announcing a run that is not
    # recorded as running is a lie a subscriber cannot check.
    from turncall.domain.enums import CallEventType

    await _dispatch_run_event(
        session,
        event_type=CallEventType.EVAL_RUN_STARTED,
        run_id=run.id,
        project_id=run.project_id,
        agent_id=target.agent_id,
        payload={
            "scenario_id": str(run.scenario_id) if run.scenario_id else None,
            "scenario_name": run.scenario_name,
            "kind": kind.value,
            "modality": modality.value,
            "iterations": run.iterations,
            "batch_id": str(run.batch_id) if run.batch_id else None,
            "target": sanitize_target(run.target),
            "agent_id": str(target.agent_id) if target.agent_id else None,
            "agent_version": target.agent_version,
        },
    )

    return IterationPlan(
        parsed=parsed,
        kind=kind,
        target=target,
        modality=modality,
        settings=settings,
        session_factory=session_factory,
        run_id=run.id,
        tool_mocks=tool_mocks,
        live_tools=live_tools,
        timeout_s=iteration_budget_seconds(settings, run.iterations),
        credentials=credentials,
    )


async def _finish_run(
    session_factory: Any, plan: IterationPlan, outcomes: list[IterationOutcome]
) -> None:
    """Write the run's verdict, its counts and every iteration's entry, then
    announce it (#76)."""
    from turncall.domain.enums import CallEventType
    from turncall.storage.repositories import eval_repo

    run_id = plan.run_id

    status, passed, failed = derive_status(outcomes)
    error = None
    if status is EvalRunStatus.ERRORED:
        refused = next((o.refused_tool for o in outcomes if o.refused_tool), None)
        error = (
            f"unmocked tool: {refused}"
            if refused
            else next(
                (o.entry.get("error") for o in outcomes if o.entry.get("error")),
                "no iteration produced a verdict",
            )
        )

    async with session_factory() as session:
        written = await eval_repo.finish_run(
            session,
            run_id,
            status=status,
            passed_count=passed,
            failed_count=failed,
            results=[o.entry for o in outcomes],
            error=error,
        )
        await session.commit()
        if not written:
            # The row reached a terminal status without this loop — cancelled
            # by the user, or reclaimed by the janitor. Announcing a verdict
            # for a conversation someone asked to stop is the whole complaint
            # in #92; the run they cancelled stays cancelled.
            logger.info("eval_run_verdict_discarded", run_id=str(run_id))
            return

        # Read back rather than reassembling: an event that disagrees with the
        # row is the kind of bug nobody finds for months.
        finished = await eval_repo.get_run(session, run_id)
        if finished is not None:
            await _dispatch_run_event(
                session,
                event_type=CallEventType.EVAL_RUN_COMPLETED,
                run_id=run_id,
                project_id=plan.target.project_id,
                agent_id=plan.target.agent_id,
                payload=_completed_payload(finished),
            )

    logger.info(
        "eval_run_finished",
        run_id=str(run_id),
        status=status.value,
        passed=passed,
        failed=failed,
    )


def batch_outcome(batch_id: UUID, runs: list[Any]) -> dict[str, Any]:
    """A batch's verdict and counts, from the run rows alone (#75).

    Derived the way a run derives its own from its iterations, one level out:
    anything still queued or running makes the batch `running`, any failed run
    fails it, and a batch where nothing reached a verdict is `errored` rather
    than passed — `errored` is not a kind of pass, at either level.

    `passed_count`/`failed_count` are runs, not iterations: a batch of ten
    scenarios reads 9/10, and each run still reports its own 7/10 inside.
    """
    counts: dict[str, int] = {}
    for run in runs:
        counts[run.status] = counts.get(run.status, 0) + 1

    in_flight = counts.get(EvalRunStatus.QUEUED.value, 0) + counts.get(
        EvalRunStatus.RUNNING.value, 0
    )
    passed = counts.get(EvalRunStatus.PASSED.value, 0)
    failed = counts.get(EvalRunStatus.FAILED.value, 0)
    if in_flight:
        status = EvalRunStatus.RUNNING
    elif failed:
        status = EvalRunStatus.FAILED
    elif passed:
        status = EvalRunStatus.PASSED
    elif counts.get(EvalRunStatus.CANCELLED.value, 0) and not counts.get(
        EvalRunStatus.ERRORED.value, 0
    ):
        status = EvalRunStatus.CANCELLED
    else:
        status = EvalRunStatus.ERRORED

    return {
        "batch_id": batch_id,
        "status": status,
        "total": len(runs),
        "counts": counts,
        "passed_count": passed,
        "failed_count": failed,
        "runs": runs,
    }


async def execute_run(
    run_id: UUID,
    *,
    session_factory: Any,
    settings: Any,
    execute: ExecuteIteration | None = None,
) -> None:
    """Run one queued eval run to a terminal status.

    Never raises: a run that blows up is recorded as `errored` with the reason,
    because a worker that dies on a bad scenario stops serving every other one.

    The session is closed before the first iteration and reopened to write the
    verdict: a run can take minutes, and holding a connection through it would
    starve the pool a worker shares with everything else it is running.
    """
    from turncall.storage.repositories import eval_repo

    if execute is None:
        from turncall.evals.harness import run_iteration

        execute = run_iteration

    async with session_factory() as session:
        run = await eval_repo.get_run(session, run_id)
        if run is None:
            logger.warning("eval_run_missing", run_id=str(run_id))
            return
        if run.status != EvalRunStatus.QUEUED.value:
            # Cancelled while queued, or already claimed by another worker.
            logger.info("eval_run_not_queued", run_id=str(run_id), status=run.status)
            return

        iterations = run.iterations
        plan = await _plan_run(
            session, run, settings=settings, session_factory=session_factory
        )
        if plan is None:
            return

    # The judge and persona are built by pipecat from a factory path, so the
    # project's keys reach them through the context rather than an argument.
    with credentials_scope(plan.credentials):
        outcomes = await run_iterations(plan, iterations=iterations, execute=execute)
    await _finish_run(session_factory, plan, outcomes)
