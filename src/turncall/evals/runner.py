"""Running one eval run: resolve the target, iterate, derive a verdict.

This module is the seam the worker calls and the unit tests drive. Everything
that needs a pipeline and a socket lives behind the injected `execute` callable
(`turncall.evals.harness.run_iteration` in production), so the loop, the status
derivation, the counts and the result mapping are testable with canned pipecat
result objects.

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
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from turncall.domain.enums import EvalKind, EvalModality, EvalRunStatus
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod

# Bot speech, as pipecat names the events that carry it. `llm_response` is text
# modality, `response`/`tts_response` audio.
_BOT_SPEECH_EVENTS = ("llm_response", "response", "tts_response")


class TargetError(ValueError):
    """The run names a target that cannot be resolved."""


class ScenarioKindError(ValueError):
    """The run names a scenario kind this build cannot execute."""


@dataclass(frozen=True)
class ResolvedTarget:
    """What the run actually points at, snapshot included.

    `agent_id` is None for an inline target — ADR-0017's first rule, no locally
    invented sentinel in the column.
    """

    project_id: UUID
    config: AgentConfig
    config_blob: dict[str, Any]
    agent_id: UUID | None = None


@dataclass(frozen=True)
class IterationOutcome:
    """One iteration's contribution to the run.

    `passed` is None when the harness did not complete, which is the whole
    point of the type: that iteration counts toward neither rate.
    """

    passed: bool | None
    entry: dict[str, Any]


ExecuteIteration = Callable[..., Awaitable[Any]]


async def resolve_target(
    session: Any, *, project_id: UUID, target: dict[str, Any]
) -> ResolvedTarget:
    """Resolve a run's target into the config that will actually run.

    Slice #70 resolves `{"type": "agent", "agent_id": ...}` only; inline and
    latest-published targets are #74.
    """
    kind = target.get("type")
    if kind != "agent":
        raise TargetError(
            f"unsupported target type {kind!r} — only 'agent' is supported yet"
        )
    raw_id = target.get("agent_id")
    if not raw_id:
        raise TargetError("target type 'agent' needs an 'agent_id'")

    from turncall.storage.repositories import agent_repo

    agent = await agent_repo.get_agent_by_id(
        session, UUID(str(raw_id)), project_id=project_id
    )
    if agent is None:
        raise TargetError(f"agent {raw_id} not found in this project")

    blob = dict(agent.config_blob or {})
    return ResolvedTarget(
        project_id=project_id,
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=agent.id,
    )


def harness_config(parsed: Any = None) -> dict[str, Any]:
    """What the harness side of the run was, for the run's third snapshot.

    The judge decides the verdict, so a result cannot be compared across time
    without knowing which model answered — a silent provider-side model update
    moves the whole baseline, and pipecat's own default (ollama/gemma) is not
    the one most people think they are running. Read off the parsed scenario
    rather than passed in, because `judge.eval:` is the scenario's to set and
    pipecat fills the default when it does not.

    Args:
        parsed: The pipecat scenario about to run, or None before one exists.
    """
    from importlib.metadata import version

    try:
        pipecat_version = version("pipecat-ai")
    except Exception:  # pragma: no cover - packaging metadata is always there
        pipecat_version = "unknown"
    judge = getattr(parsed, "judge", None) or {}
    return {
        "pipecat_version": pipecat_version,
        # Pipecat fills `judge.eval:` with its ollama default whether or not a
        # judge is ever built, so record whether one actually decided anything.
        # A snapshot naming a model that never ran is the same lie as a policy
        # that was never enforced.
        "judge_used": _uses_judge(parsed),
        "judge_service": judge.get("service"),
        "judge_model": judge.get("model"),
        # A custom judge is a dotted path instead of a service/model pair; with
        # neither recorded the snapshot would say nothing at all about it.
        "judge_factory": judge.get("factory"),
        "schema_version": scenario_mod.SCHEMA_VERSION,
    }


def _uses_judge(parsed: Any) -> bool:
    """Whether anything in this scenario actually asks the judge a question."""
    for turn in getattr(parsed, "turns", []) or []:
        if any(getattr(exp, "eval", None) is not None for exp in turn.expect or []):
            return True
    # A simulation is judged end to end by definition (#73).
    return getattr(parsed, "persona", None) is not None


# Flipped to True by #71, when the tool bridge actually short-circuits. Until
# then every snapshot has to say so: the scenario column defaults to
# `mock_only`, and copying that into a durable record without qualification
# would tell a future reader the run's tools were mocked when they fired for
# real. That is the same lie the API now rejects, one layer down and permanent.
TOOL_POLICY_ENFORCED = False


def resolved_scenario_snapshot(
    *,
    definition: dict[str, Any],
    schema_version: str,
    tool_mocks: dict[str, Any],
    tool_policy: str,
) -> dict[str, Any]:
    """The scenario exactly as it ran, mocks and policy included.

    The mocks are part of what the test means, so a run that does not record
    them cannot be compared with the next one — and whether the policy was
    honoured is part of that, not a detail.
    """
    return {
        "definition": definition,
        "schema_version": schema_version,
        "tool_mocks": tool_mocks,
        "tool_policy": tool_policy,
        "tool_policy_enforced": TOOL_POLICY_ENFORCED,
    }


def _spoken(event: dict) -> str:
    return event.get("text") or event.get("transcript") or ""


def _transcript_from_script(result: Any, parsed: Any) -> list[dict[str, str]]:
    """The conversation, as the user turns that were actually sent interleaved
    with what the bot actually said.

    Two things this must not do, both learned by measuring rather than
    reasoning. It must not invent user speech: `stop_on_failure` is the default,
    so a failing run leaves its later turns `not_run`, and rendering their
    scripted text would show the caller saying things the harness never sent.
    And it must not lose bot speech: a failing turn matches no expectation, so
    building the bot side from `expectation.matched` blanked the transcript
    exactly when someone needs to read it.

    Attribution is by order, one reply per scored turn. An earlier attempt
    windowed by the running total of `duration_ms`, which is simply wrong:
    `at` is measured from the harness's start and includes a connect and
    handshake that no turn's duration accounts for, so every reply landed a
    turn or more late.

    ponytail: one-per-turn is exact for text modality, where `llm_response` is
    emitted once per response. Audio's `tts_response` is one event per spoken
    segment, so #72 needs to revisit this — leftovers are appended rather than
    dropped, which keeps the content complete meanwhile.
    """
    turns = list(getattr(parsed, "turns", []) or [])
    events = [
        event
        for event in getattr(result, "events_seen", []) or []
        if event.get("type") in _BOT_SPEECH_EVENTS and _spoken(event)
    ]

    transcript: list[dict[str, str]] = []
    consumed = 0
    for turn_result in getattr(result, "turns", []) or []:
        if turn_result.status == "not_run":
            # The run stopped before this turn. Nothing was sent, nothing said.
            continue
        index = turn_result.turn_index
        if index < len(turns):
            turn = turns[index]
            if turn.user:
                transcript.append({"role": "user", "content": turn.user})
            elif getattr(turn, "dtmf", None):
                # A keypress is the caller's turn too; pipecat's own judge
                # records it the same way.
                transcript.append(
                    {"role": "user", "content": f"(DTMF keypad input: {turn.dtmf})"}
                )
        if consumed < len(events):
            transcript.append(
                {"role": "assistant", "content": _spoken(events[consumed])}
            )
            consumed += 1

    # A turn that said several things, or speech after the last scored turn:
    # kept rather than dropped, since losing it is the bug this replaced.
    transcript.extend(
        {"role": "assistant", "content": _spoken(event)} for event in events[consumed:]
    )
    return transcript


def _failures(items: Any) -> list[dict[str, Any]]:
    return [
        {
            "turn_index": f.turn_index,
            "expectation_index": f.expectation_index,
            "event_name": f.event_name,
            "kind": f.kind,
            "reason": f.reason,
        }
        for f in items or []
    ]


def _entry(iteration: int, **over: Any) -> dict[str, Any]:
    """One `results` entry, so every iteration reads the same whatever happened.

    A reader should not have to know whether an iteration ran, was skipped or
    blew up to find its transcript key.
    """
    return {
        "iteration": iteration,
        "passed": None,
        "duration_ms": 0,
        "transcript": [],
        "failures": [],
        "turns": [],
        "skipped": None,
        "error": None,
        **over,
    }


def map_script_result(result: Any, *, iteration: int, parsed: Any) -> IterationOutcome:
    """Turn pipecat's `EvalScriptResult` into one entry of the run's results.

    A skipped result is neither passed nor failed — pipecat draws that line
    itself, and so do we.
    """
    skipped = getattr(result, "skipped", None)
    passed = None if skipped is not None else bool(result.passed)
    entry = _entry(
        iteration,
        passed=passed,
        duration_ms=getattr(result, "duration_ms", 0),
        transcript=_transcript_from_script(result, parsed),
        failures=_failures(getattr(result, "failures", [])),
        turns=[
            {
                "index": t.turn_index,
                "status": t.status,
                "duration_ms": t.duration_ms,
                "failures": _failures(t.failures),
                "expectations": [
                    {
                        "index": e.expectation_index,
                        "event_name": e.event_name,
                        "passed": e.passed,
                        "matched": e.matched,
                    }
                    for e in t.expectations or []
                ],
            }
            for t in getattr(result, "turns", []) or []
        ],
        skipped=skipped,
    )
    return IterationOutcome(passed=passed, entry=entry)


def errored_entry(iteration: int, reason: str) -> IterationOutcome:
    """An iteration the harness could not complete."""
    return IterationOutcome(passed=None, entry=_entry(iteration, error=reason))


def derive_status(outcomes: list[IterationOutcome]) -> tuple[EvalRunStatus, int, int]:
    """The run's verdict and its two counts.

    No iteration reached a verdict -> `errored`: the harness never told us
    anything about the agent, and reporting that as a failure is how a suite
    loses its audience. Otherwise any failed iteration fails the run, since a
    scripted scenario that fails once has found something real.
    """
    passed = sum(1 for o in outcomes if o.passed is True)
    failed = sum(1 for o in outcomes if o.passed is False)
    if passed == 0 and failed == 0:
        return EvalRunStatus.ERRORED, 0, 0
    status = EvalRunStatus.FAILED if failed else EvalRunStatus.PASSED
    return status, passed, failed


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

        project_id = run.project_id
        modality = EvalModality(run.modality)
        kind = EvalKind.SCRIPTED
        iterations = run.iterations
        definition = dict(run.resolved_scenario.get("definition") or {})
        scenario_name = run.scenario_name

        try:
            # The definition decides, not `run.kind`. The column is a copy made
            # at queue time; if the two ever disagree, the mapper is chosen from
            # what actually parses, and `map_script_result` would otherwise be
            # handed a simulation result to read.
            kind = scenario_mod.kind_of(definition)
            if kind is not EvalKind.SCRIPTED:
                raise ScenarioKindError(
                    f"{kind.value!r} scenarios cannot be run yet — scripted only"
                )
            merged = scenario_mod.with_modality(definition, modality)
            parsed = scenario_mod.parse(merged, name=scenario_name)
            target = await resolve_target(
                session, project_id=project_id, target=run.target
            )
        except Exception as exc:
            await eval_repo.finish_run(
                session,
                run_id,
                status=EvalRunStatus.ERRORED,
                passed_count=0,
                failed_count=0,
                results=[],
                error=str(exc),
            )
            await session.commit()
            logger.warning("eval_run_unrunnable", run_id=str(run_id), error=str(exc))
            return

        if target.config.tools or target.config.mcp_servers:
            # tool_mocks/tool_policy are stored but not yet enforced (#71), so
            # this agent's tools fire for real on every iteration.
            logger.warning(
                "eval_agent_has_live_tools",
                run_id=str(run_id),
                agent_id=str(target.agent_id),
                tools=len(target.config.tools),
                mcp_servers=len(target.config.mcp_servers),
            )

        await eval_repo.start_run(
            session,
            run_id,
            resolved_config=target.config_blob,
            agent_id=target.agent_id,
            harness_config=harness_config(parsed),
        )
        await session.commit()

    outcomes: list[IterationOutcome] = []
    for iteration in range(1, iterations + 1):
        try:
            result = await execute(
                parsed=parsed,
                kind=kind,
                target=target,
                modality=modality,
                settings=settings,
                session_factory=session_factory,
                run_id=run_id,
            )
        except Exception as exc:
            logger.exception("eval_iteration_error", run_id=str(run_id))
            outcomes.append(errored_entry(iteration, f"{type(exc).__name__}: {exc}"))
            continue
        outcomes.append(map_script_result(result, iteration=iteration, parsed=parsed))

    status, passed, failed = derive_status(outcomes)
    error = None
    if status is EvalRunStatus.ERRORED:
        error = next(
            (o.entry.get("error") for o in outcomes if o.entry.get("error")),
            "no iteration produced a verdict",
        )

    async with session_factory() as session:
        await eval_repo.finish_run(
            session,
            run_id,
            status=status,
            passed_count=passed,
            failed_count=failed,
            results=[o.entry for o in outcomes],
            error=error,
        )
        await session.commit()

    logger.info(
        "eval_run_finished",
        run_id=str(run_id),
        status=status.value,
        passed=passed,
        failed=failed,
    )
