"""Turning pipecat's eval results into one run's `results` entries.

Split out of `runner.py`, which had grown past this project's file ceiling. The
seam is a real one and not just a line count: everything here is a pure
function of a pipecat result object, so it needs no session, no settings and no
pipeline, and the two mappers are the only place this project reads pipecat's
result shapes.

The rule the whole module turns on is that the two kinds are mapped by two
separate functions. A scripted result answers "at this point, did the agent make
the right next decision?" and a simulation "by the end, did it reach the right
outcome?" — handing one's result to the other's mapper reports nonsense
confidently, which is why they were split in the first place. `_entry` is what
keeps them one shape to read: every key exists on every entry, so nobody has to
know which kind they are holding to find a transcript.

`errored` is not a kind of `failed`. An iteration whose harness did not complete
is neither a pass nor a fail, and `IterationOutcome.passed` is None to say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

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


def _confidence(source: Any) -> float | None:
    """How sure the judge was of this verdict, or None when it said nothing.

    Read by `getattr` on purpose, at every site a judge's answer lands. Pipecat
    1.12 carries a confidence on `JudgeVerdict` (a classifier's probability,
    0..1) but drops it building the result objects we read — the matcher for a
    scripted expectation, `_judge_conversation` for a simulation's turn
    verdicts and goal — so today this is absent on every real run and lights up
    with no change here when pipecat threads it through.

    Absent is **not** zero: zero is "the judge was certain and said the
    opposite", which is the strongest verdict there is, and a judge that
    reports no confidence (`text_contains`, a `function_call`, a measured
    metric — none of which involve a judge at all) has made no such claim.

    Only a real probability is stored. Pipecat types this `float | None` in
    0..1, and everything else it could become is a worse lie than absence: a
    `bool` is an `int`, so `True` would read as the certainty at the top of the
    scale, and `nan`/`inf`/`5.0` would ship to a subscriber as a probability.
    Unusable is absent — never an exception that loses a whole run's results —
    but logged, because a value that is present and unusable can only mean
    pipecat's shape moved, and that is worth finding from a log line rather
    than from a column of nulls.
    """
    value = getattr(source, "confidence", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("eval_confidence_not_a_number", value=repr(value))
        return None
    number = float(value)
    # `nan` fails this comparison, which is the answer it should get.
    if not 0.0 <= number <= 1.0:
        logger.warning("eval_confidence_out_of_range", confidence=number)
        return None
    return number


def _failures(items: Any) -> list[dict[str, Any]]:
    return [
        {
            "turn_index": f.turn_index,
            "expectation_index": f.expectation_index,
            "event_name": f.event_name,
            "kind": f.kind,
            "reason": f.reason,
            "confidence": _confidence(f),
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
                        "confidence": _confidence(e),
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
                "confidence": _confidence(v),
            }
            for v in getattr(metric, "verdicts", []) or []
        ],
    }


def _simulation_failures(result: Any) -> list[dict[str, Any]]:
    """Why this iteration did not pass, goal first then each short metric.

    Every entry carries a `confidence` because the scripted mapper's failures
    do, and `_entry`'s rule is that a reader should not have to know which kind
    it is holding to find a key. The goal's is the judge's verdict on the goal;
    a metric has none of its own — its verdicts are per bot turn, each with its
    own confidence under `metrics[].verdicts[]`, and averaging them into one
    number here is the aggregate #138 deliberately does not build.
    """
    failures: list[dict[str, Any]] = []
    if not result.succeeded:
        failures.append(
            {
                "kind": "goal",
                "name": "success",
                "reason": result.reason or "",
                "confidence": _confidence(result),
            }
        )
    failures.extend(
        {
            "kind": "metric",
            "name": m.name,
            "reason": m.reason or "",
            "confidence": None,
        }
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
        goal={
            "succeeded": bool(result.succeeded),
            "reason": result.reason or "",
            # The goal verdict is flattened onto the result itself (`succeeded`
            # + `reason`), so its confidence reads from there too.
            "confidence": _confidence(result),
        },
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
