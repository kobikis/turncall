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

from turncall.domain.enums import (
    EvalKind,
    EvalModality,
    EvalRunStatus,
    EvalToolPolicy,
)
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.services.tool_mocks import ToolMocks

# What the bot said, as pipecat names it. In text modality there is only
# `llm_response`. In audio there are three views of one reply: `llm_response`
# (what the model wrote), `tts_response` (what the TTS reports speaking, one
# event per segment), and `response` (the harness's own transcription of the
# audio it actually heard). The judge reads `response`, so that is the
# transcript — with the model's own text kept beside it, because when an audio
# run fails where a text run passed, the difference between those two *is* the
# explanation (#72).
_JUDGE_EVENT = "response"
_LLM_EVENT = "llm_response"


class TargetError(ValueError):
    """The run names a target that cannot be resolved."""


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
    # Which version `agent_id` was. None for an inline target, which has no
    # row and therefore no version (#74).
    agent_version: int | None = None


@dataclass(frozen=True)
class IterationOutcome:
    """One iteration's contribution to the run.

    `passed` is None when the harness did not complete, which is the whole
    point of the type: that iteration counts toward neither rate.
    """

    passed: bool | None
    entry: dict[str, Any]
    # The tool a `mock_only` run asked for and had no mock for. Set on this
    # iteration, but it condemns the whole run: the scenario is misconfigured,
    # not the agent (#71).
    refused_tool: str | None = None


ExecuteIteration = Callable[..., Awaitable[Any]]


# An agent row is one immutable version, so a target naming an id pins a
# version forever: the moment someone publishes the next one, a scenario
# pinned that way silently stops testing production. `agent_name` is the
# answer — resolved at run time, recorded as the version it found.
_TARGET_TYPES = ("agent", "agent_name", "inline")


async def _resolve_by_id(
    session: Any, *, project_id: UUID, raw_id: Any
) -> ResolvedTarget:
    from turncall.storage.repositories import agent_repo

    if not raw_id:
        raise TargetError("target type 'agent' needs an 'agent_id'")
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
        agent_version=agent.version,
    )


async def _resolve_by_name(
    session: Any, *, project_id: UUID, name: Any, environment: str
) -> ResolvedTarget:
    from turncall.storage.repositories import agent_repo

    if not name:
        raise TargetError("target type 'agent_name' needs a 'name'")
    agent = await agent_repo.get_latest_published(
        session, project_id, str(name), environment
    )
    if agent is None:
        # Deliberately not falling back to a draft: "test what is live" is the
        # whole reason to target by name, and quietly testing an unpublished
        # draft instead would answer a different question with the same green
        # tick.
        raise TargetError(f"agent {name!r} has no published version in {environment!r}")
    blob = dict(agent.config_blob or {})
    return ResolvedTarget(
        project_id=project_id,
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=agent.id,
        agent_version=agent.version,
    )


def _resolve_inline(*, project_id: UUID, blob: Any) -> ResolvedTarget:
    """An agent config with no row behind it — ADR-0017's case.

    `agent_id` stays None rather than carrying the zero-UUID sentinel: the
    sentinel exists so a *pipeline* can build, and writing it to a column would
    claim a row that does not exist. The config itself is the record, which is
    what `resolved_config` is for.
    """
    if not isinstance(blob, dict) or not blob:
        raise TargetError("target type 'inline' needs an 'agent' configuration")
    try:
        config = AgentConfig.model_validate(blob)
    except Exception as exc:
        # Reached only if something slipped past the API's own validation —
        # a run queued before a schema change, say. Better a clear errored run
        # than a pipeline that fails halfway into a conversation.
        raise TargetError(f"inline agent configuration is invalid: {exc}") from exc
    return ResolvedTarget(
        project_id=project_id,
        config=config,
        config_blob=dict(blob),
        agent_id=None,
        agent_version=None,
    )


async def resolve_target(
    session: Any, *, project_id: UUID, target: dict[str, Any]
) -> ResolvedTarget:
    """Resolve a run's target into the config that will actually run.

    Three forms (#74): a pinned `agent_id`, an `agent_name` resolved to
    whatever is published right now, and an `inline` config with no row at all
    — which is also the sandbox an operator points a scenario at when its tools
    have real side effects.
    """
    kind = target.get("type")
    if kind == "agent":
        return await _resolve_by_id(
            session, project_id=project_id, raw_id=target.get("agent_id")
        )
    if kind == "agent_name":
        return await _resolve_by_name(
            session,
            project_id=project_id,
            name=target.get("name"),
            environment=str(target.get("environment") or "production"),
        )
    if kind == "inline":
        return _resolve_inline(project_id=project_id, blob=target.get("agent"))
    raise TargetError(
        f"unsupported target type {kind!r} — one of {', '.join(_TARGET_TYPES)}"
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
        # Audio only. The caller's voice and the STT the judge read through are
        # as much a part of a result as the judge model: a different TTS says
        # the same line differently, and a different STT mishears it
        # differently. A snapshot without them cannot explain a flip (#72).
        "user_speech": getattr(parsed, "user_speech", None),
        "bot_transcription": getattr(parsed, "transcriber", None),
        "schema_version": scenario_mod.SCHEMA_VERSION,
    }


def _uses_judge(parsed: Any) -> bool:
    """Whether anything in this scenario actually asks the judge a question."""
    for turn in getattr(parsed, "turns", []) or []:
        if any(getattr(exp, "eval", None) is not None for exp in turn.expect or []):
            return True
    # A simulation is judged end to end by definition (#73).
    return getattr(parsed, "persona", None) is not None


# The tool bridge short-circuits on the scenario's mocks and fails closed on
# the policy (#71), so a snapshot saying `mock_only` now means it. Kept in the
# snapshot rather than dropped: runs written before that slice say `False`, and
# a reader comparing across it needs to know which of the two they are holding.
TOOL_POLICY_ENFORCED = True


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


def _bot_turns(result: Any) -> list[dict[str, str]]:
    """One entry per reply the bot gave, in order.

    `content` is what the judge read: its transcription of the bot's audio when
    the run made the bot speak, the model's text when it did not. In audio the
    model's text rides along as `text` whenever the two are both present and
    differ — a wrong word there is STT, a wrong sentence is the agent, and
    without both in the record every audio failure is a mystery.

    Attribution between the two lists is by order. `tts_response` is
    deliberately not a source: it is emitted per spoken *segment*, so one reply
    can raise several, and mixing it in gave one reply three transcript lines.
    """
    events = getattr(result, "events_seen", []) or []
    heard = [e for e in events if e.get("type") == _JUDGE_EVENT and _spoken(e)]
    written = [e for e in events if e.get("type") == _LLM_EVENT and _spoken(e)]

    if not heard:
        return [{"role": "assistant", "content": _spoken(e)} for e in written]

    turns = []
    for index, event in enumerate(heard):
        entry = {"role": "assistant", "content": _spoken(event)}
        text = _spoken(written[index]) if index < len(written) else ""
        if text and text != entry["content"]:
            entry["text"] = text
        turns.append(entry)
    return turns


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
    """
    turns = list(getattr(parsed, "turns", []) or [])
    replies = _bot_turns(result)

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
        if consumed < len(replies):
            transcript.append(replies[consumed])
            consumed += 1

    # A reply after the last scored turn: kept rather than dropped, since
    # losing bot speech is the bug this replaced.
    transcript.extend(replies[consumed:])
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
        "tool_calls": [],
        "skipped": None,
        "error": None,
        # Simulation-only (#73), null on a scripted iteration. One shape for
        # both kinds: a reader should not have to know which it is holding to
        # find a key, which is the same argument `skipped` is here for.
        "goal": None,
        "metrics": [],
        "ended_by": None,
        "persona_turns": None,
        "persona_claim": None,
        **over,
    }


def map_script_result(
    result: Any,
    *,
    iteration: int,
    parsed: Any,
    tool_calls: list[dict[str, Any]] | None = None,
) -> IterationOutcome:
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
        tool_calls=list(tool_calls or []),
    )
    return IterationOutcome(passed=passed, entry=entry)


def _metric(metric: Any) -> dict[str, Any]:
    """One quality metric's outcome.

    Reported whether or not it failed anything: a metric with no `min_score`
    never fails a run by design, and dropping it would leave the operator who
    asked to watch it with nothing to read.
    """
    return {
        "name": metric.name,
        # None when there was no bot turn to judge — not zero, which would read
        # as "every turn failed".
        "score": metric.score,
        "passed": metric.passed,
        "reason": metric.reason,
        "min_score": metric.min_score,
        # A measured metric (turns, duration, words, latency, function_calls)
        # carries a value and no verdicts; a judged one the reverse.
        "value": getattr(metric, "value", None),
        "failure_kind": getattr(metric, "failure_kind", None),
        "verdicts": [
            {
                "turn": v.turn,
                "passed": v.passed,
                "verdict": v.verdict,
                "reason": v.reason,
            }
            for v in getattr(metric, "verdicts", []) or []
        ],
    }


def _simulation_failures(result: Any) -> list[dict[str, Any]]:
    """Why this iteration did not pass, goal first then each short metric."""
    failures: list[dict[str, Any]] = []
    if not result.succeeded:
        failures.append(
            {"kind": "goal", "name": "success", "reason": result.reason or ""}
        )
    failures.extend(
        {"kind": "metric", "name": m.name, "reason": m.reason or ""}
        for m in result.metrics or []
        if not m.passed
    )
    return failures


def _persona_claim(result: Any) -> dict[str, Any] | None:
    """The simulated caller's own end-of-call verdict, marked advisory.

    Pipecat is explicit that the judge decides; the persona's `end_call` claim
    is what the tester *thinks* happened. Worth storing — it is the fastest way
    to spot a persona that wandered off its goal — but presenting the two as
    equals teaches people to distrust the judge, so the label travels with the
    value rather than living in a UI that may not carry it.
    """
    claim = getattr(result, "end_call", None)
    if not claim:
        return None
    return {
        "success": claim.get("success"),
        "reason": claim.get("reason", ""),
        "advisory": True,
    }


def map_simulation_result(
    result: Any,
    *,
    iteration: int,
    tool_calls: list[dict[str, Any]] | None = None,
) -> IterationOutcome:
    """Turn pipecat's `EvalSimulationResult` into one entry of the run's results.

    `error` is pipecat's own word for "the run did not complete" — a failed
    connect, a persona LLM that died mid-conversation. That is `errored` here
    too, counting toward neither rate: the conversation never finished, so it
    says nothing about the agent. Everything else is a verdict, and
    `result.passed` is pipecat's: the goal was met *and* no metric fell short.
    """
    if getattr(result, "error", None):
        return errored_entry(iteration, str(result.error), tool_calls=tool_calls)

    passed = bool(result.passed)
    entry = _entry(
        iteration,
        passed=passed,
        duration_ms=getattr(result, "duration_ms", 0),
        # The persona's turns are `user`, the bot's `assistant` — already the
        # shape a scripted transcript is built into, so one reader serves both.
        transcript=[
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in getattr(result, "messages", []) or []
        ],
        failures=_simulation_failures(result),
        tool_calls=list(tool_calls or []),
        goal={"succeeded": bool(result.succeeded), "reason": result.reason or ""},
        metrics=[_metric(m) for m in result.metrics or []],
        # How the conversation ended: `end_call` is the persona hanging up,
        # `max_turns`/`max_duration`/`silence` are backstops, and a run they
        # ended has not succeeded.
        ended_by=getattr(result, "ended_by", None),
        persona_turns=getattr(result, "turns", 0),
        persona_claim=_persona_claim(result),
    )
    return IterationOutcome(passed=passed, entry=entry)


def errored_entry(
    iteration: int,
    reason: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    refused_tool: str | None = None,
) -> IterationOutcome:
    """An iteration the harness could not complete."""
    return IterationOutcome(
        passed=None,
        entry=_entry(iteration, error=reason, tool_calls=list(tool_calls or [])),
        refused_tool=refused_tool,
    )


def derive_status(outcomes: list[IterationOutcome]) -> tuple[EvalRunStatus, int, int]:
    """The run's verdict and its two counts.

    No iteration reached a verdict -> `errored`: the harness never told us
    anything about the agent, and reporting that as a failure is how a suite
    loses its audience. Otherwise any failed iteration fails the run, since a
    scripted scenario that fails once has found something real.

    An unmocked tool errors the **run**, whatever the other iterations did
    (#71). The agent asked for something the scenario forgot to mock, so the
    scenario is misconfigured and no rate over it means anything — and a
    forgotten mock that reported PASSED because four other iterations never
    reached the tool is exactly the accident the policy exists to prevent.
    """
    passed = sum(1 for o in outcomes if o.passed is True)
    failed = sum(1 for o in outcomes if o.passed is False)
    if any(o.refused_tool for o in outcomes):
        return EvalRunStatus.ERRORED, 0, 0
    if passed == 0 and failed == 0:
        return EvalRunStatus.ERRORED, 0, 0
    status = EvalRunStatus.FAILED if failed else EvalRunStatus.PASSED
    return status, passed, failed


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
    except Exception:
        logger.warning("eval_cancel_check_failed", run_id=str(plan.run_id))
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
        try:
            result = await execute(**plan.execute_kwargs(), tool_mocks=mocks)
        except Exception as exc:
            logger.exception("eval_iteration_error", run_id=str(plan.run_id))
            outcomes.append(
                errored_entry(
                    iteration, f"{type(exc).__name__}: {exc}", tool_calls=mocks.calls
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
            outcomes.append(
                map_simulation_result(
                    result, iteration=iteration, tool_calls=mocks.calls
                )
            )
        else:
            outcomes.append(
                map_script_result(
                    result,
                    iteration=iteration,
                    parsed=plan.parsed,
                    tool_calls=mocks.calls,
                )
            )
    return outcomes


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
        "target": run.target,
        # Null for an inline target — the honest answer to "which stored agent
        # was this", not missing data (ADR-0017).
        "agent_id": str(run.agent_id) if run.agent_id else None,
        "agent_version": run.agent_version,
        "results": run.results,
        # What the worker's log used to be the only home for (#96).
        "warnings": list(run.warnings or []),
        "resolved_config": run.resolved_config,
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
    from turncall.events.dispatcher import dispatch_event

    try:
        await dispatch_event(
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
        error=str(exc),
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


def _warn_unmatched_mocks(
    tool_mocks: dict[str, Any], target: ResolvedTarget, *, run_id: UUID
) -> list[dict[str, Any]]:
    """Say so when a mock names a tool this run can never call.

    An eval builds its pipeline through `build_call_pipeline`, which takes no
    MCP manager, so an agent's MCP servers are neither connected nor
    discovered: nothing is contacted (which is the point), but the model is
    never shown those tools either, and a mock keyed to one sits there meaning
    nothing. A typo in a mock's name looks exactly the same. Silence is the
    worst of the three, since a mock that never fires reads as a tool that was
    never called.

    ponytail: a warning, not a rejection — the agent can be edited after the
    scenario was written, and failing a queued run over a stale mock helps
    nobody. Advertising mocked-but-undiscovered tools to the model is the real
    fix, and needs a schema the mock does not carry today (#74's inline targets
    are the sanctioned way to give an eval a tool surface of its own).
    """
    if not tool_mocks:
        return []
    from turncall.domain.models import BUILTIN_TOOL_NAMES

    known = {t.name for t in target.config.tools} | set(BUILTIN_TOOL_NAMES)
    unmatched = sorted(set(tool_mocks) - known)
    if not unmatched:
        return []
    servers = len(target.config.mcp_servers)
    logger.warning(
        "eval_mock_matches_no_tool",
        run_id=str(run_id),
        tools=unmatched,
        mcp_servers=servers,
    )
    # Returned as well as logged (#96): the log is the worker's container and
    # the author reads the run. The MCP server count is the valuable half —
    # it is what says "this is the MCP limitation, not your typo".
    return [
        {
            "code": "mock_matches_no_tool",
            "message": (
                f"{len(unmatched)} mock(s) name a tool this run cannot call: "
                + ", ".join(unmatched)
                + (
                    f". The agent has {servers} MCP server(s), whose tools an "
                    "eval never connects or advertises — a mock naming one can "
                    "never fire."
                    if servers
                    else ". Check the tool name against the agent's tools."
                )
            ),
            "tools": unmatched,
            "mcp_servers": servers,
        }
    ]


def _parse_for_run(run: Any, modality: EvalModality) -> tuple[EvalKind, Any]:
    """The pipecat scenario this run will drive, and the kind it turned out to be.

    The definition decides, not `run.kind`. The column is a copy made at queue
    time; if the two ever disagree, the mapper is chosen from what actually
    parses, and `map_script_result` would otherwise be handed a simulation
    result to read.
    """
    definition = dict(run.resolved_scenario.get("definition") or {})
    kind = scenario_mod.kind_of(definition)
    merged = scenario_mod.with_modality(definition, modality)
    return kind, scenario_mod.parse(merged, name=run.scenario_name)


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
        kind, parsed = _parse_for_run(run, modality)
        target = await resolve_target(
            session, project_id=run.project_id, target=run.target
        )
    except Exception as exc:
        await _record_unrunnable(session, run, exc)
        return None

    tool_mocks, live_tools = tool_policy_of(run.resolved_scenario)
    # Carried into the run, not only the create response (#95): the person who
    # reads a green verdict is not always the one who wrote the scenario, and
    # "this could not have failed" is the most important thing a green run can
    # say about itself.
    warnings = scenario_mod.assertion_warnings(parsed)
    warnings += _warn_unmatched_mocks(tool_mocks, target, run_id=run.id)
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
        resolved_config=target.config_blob,
        agent_id=target.agent_id,
        agent_version=target.agent_version,
        harness_config=harness_config(parsed),
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
            "target": run.target,
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

    outcomes = await run_iterations(plan, iterations=iterations, execute=execute)
    await _finish_run(session_factory, plan, outcomes)
