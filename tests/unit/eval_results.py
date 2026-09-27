"""Canned pipecat eval results, as the mappers and the run loop see them.

Plain factories rather than pytest fixtures, imported by name — the convention
`tests/conftest.py` already sets with `mcp_tool`/`mcp_settings`. They live here
rather than there because only the eval modules want them, and they are shared
rather than copied because the mapper tests and the run loop's tests need the
same shapes: a factory duplicated into two files drifts, and then one of the two
is asserting against a result pipecat never produces.

`SimpleNamespace` on purpose. The mappers read pipecat's result objects by
attribute and must not care which class they came from, so nothing here imports
pipecat and every test built on them stays hermetic — no judge, no provider.
"""

from types import SimpleNamespace


def failure(**over):
    return SimpleNamespace(
        turn_index=2,
        expectation_index=0,
        event_name="function_call",
        kind="missing_function_call",
        reason="expected transfer_call, got none",
        **over,
    )


def expectation(matched="Hello there", event_name="llm_response", passed=True, **over):
    return SimpleNamespace(
        expectation_index=0,
        event_name=event_name,
        passed=passed,
        matched=matched,
        **over,
    )


def script_result(*, passed=True, skipped=None, turns=None, failures=None, events=None):
    return SimpleNamespace(
        scenario_name="greets-the-caller",
        passed=passed,
        skipped=skipped,
        duration_ms=4120,
        failures=failures or [],
        turns=turns if turns is not None else [],
        events_seen=events or [],
        debug_log=[],
    )


def said(text):
    """A bot-speech event as the harness records it.

    No timestamp: attribution is by order now. Windowing by the running total
    of `duration_ms` was wrong — `at` starts at the harness's connect, which no
    turn's duration accounts for.
    """
    return {"type": "llm_response", "text": text}


def turn_result(index=0, status="passed", expectations=None, failures=None):
    return SimpleNamespace(
        turn_index=index,
        status=status,
        duration_ms=900,
        expectations=expectations if expectations is not None else [expectation()],
        failures=failures or [],
    )


def metric_score(name="politeness", *, score=1.0, passed=True, min_score=1.0, **over):
    return SimpleNamespace(
        name=name,
        score=score,
        passed=passed,
        reason="every turn passed" if passed else "turn 2 was curt",
        min_score=min_score,
        verdicts=over.pop("verdicts", []),
        value=over.pop("value", None),
        failure_kind=over.pop("failure_kind", None),
        **over,
    )


def simulation_result(
    *, succeeded=True, error=None, metrics=None, messages=None, **over
):
    """What pipecat's `EvalSimulationResult` gives us. `passed` is its own
    property: the goal met *and* no metric short."""
    metrics = [] if metrics is None else metrics
    return SimpleNamespace(
        simulation_name="recovers-the-booking",
        succeeded=succeeded,
        reason=over.pop("reason", "the agent found the booking and read it back"),
        error=error,
        metrics=metrics,
        messages=messages
        if messages is not None
        else [
            {"role": "user", "content": "I lost my booking reference"},
            {"role": "assistant", "content": "I can find that for you."},
        ],
        turns=over.pop("turns", 3),
        ended_by=over.pop("ended_by", "end_call"),
        end_call=over.pop("end_call", {"success": True, "reason": "got it"}),
        duration_ms=over.pop("duration_ms", 18400),
        events_seen=[],
        debug_log=[],
        passed=error is None and succeeded and all(m.passed for m in metrics),
        **over,
    )


def parsed(*user_turns):
    return SimpleNamespace(
        turns=[SimpleNamespace(user=u, dtmf=None) for u in user_turns]
    )
