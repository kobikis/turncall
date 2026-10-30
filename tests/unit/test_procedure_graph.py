"""The rule a procedure graph is held to (#444): one pure function, applied at
the agent API (422) and at the build (`pipeline_error`). Each refusal names
the procedure and the step, so whoever reads it can find the mistake."""

from typing import Any

import pytest

from turncall.domain.procedure_graph import (
    MAX_STEPS,
    MAX_WINDOW_DEPTH,
    procedures_refusal,
)
from turncall.domain.procedures import Procedure

pytestmark = pytest.mark.unit


def _step(step_id: str, kind: str = "ask", **extra: Any) -> dict[str, Any]:
    return {"id": step_id, "kind": kind, "text": f"Do {step_id}.", **extra}


def _to(*targets: str) -> list[dict[str, str]]:
    return [{"to": t, "when": f"it is time for {t}"} for t in targets]


def _procedure(steps: list[dict[str, Any]], **extra: Any) -> Procedure:
    return Procedure.model_validate({"name": "no_internet", "steps": steps, **extra})


def _good(**extra: Any) -> Procedure:
    return _procedure(
        [
            _step("lights", start=True, moves=_to("restart", "technician")),
            _step("restart", "instruct", moves=_to("done", "technician")),
            _step("technician", "escalate"),
            _step("done", "end"),
        ],
        **extra,
    )


def test_a_sound_graph_passes():
    assert procedures_refusal([_good()]) is None
    assert procedures_refusal([]) is None


def test_a_move_to_a_step_that_does_not_exist():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("after_restart")),
            _step("after_restart", "instruct", moves=_to("technican")),
            _step("technician", "escalate"),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': step 'after_restart' moves to "
        "'technican', which does not exist"
    )


def test_a_step_no_move_reaches():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("done")),
            _step("restart", "instruct", moves=_to("done")),
            _step("done", "end"),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': step 'restart' cannot be reached from "
        "the start step 'lights'"
    )


def test_no_start_step():
    bad = _procedure([_step("lights", moves=_to("done")), _step("done", "end")])

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': no step is the start step; mark exactly "
        "one with start: true"
    )


def test_two_start_steps():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("done")),
            _step("done", "end", start=True),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': steps 'lights' and 'done' are both "
        "marked start; mark exactly one"
    )


def test_no_ending():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("restart")),
            _step("restart", "instruct", moves=_to("lights")),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': no step ends it; give it at least one "
        "step of kind 'end' or 'escalate'"
    )


def test_two_steps_with_one_id():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("done")),
            _step("done", "end"),
            _step("done", "escalate"),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': step id 'done' is used by more than one step"
    )


def test_more_steps_than_the_cap():
    steps = [
        _step(f"s{i}", start=i == 0, moves=_to(f"s{i + 1}")) for i in range(MAX_STEPS)
    ] + [_step(f"s{MAX_STEPS}", "end")]

    assert procedures_refusal([_procedure(steps)]) == (
        f"procedures[0] 'no_internet': {MAX_STEPS + 1} steps, more than the "
        f"{MAX_STEPS} a procedure may have"
    )


def test_the_cap_itself_passes():
    steps = [
        _step(f"s{i}", start=i == 0, moves=_to(f"s{i + 1}"))
        for i in range(MAX_STEPS - 1)
    ] + [_step(f"s{MAX_STEPS - 1}", "end")]

    assert procedures_refusal([_procedure(steps)]) is None


def test_a_window_deeper_than_the_cap():
    assert procedures_refusal([_good(window_depth=MAX_WINDOW_DEPTH)]) is None
    assert procedures_refusal([_good(window_depth=MAX_WINDOW_DEPTH + 1)]) == (
        f"procedures[0] 'no_internet': window_depth {MAX_WINDOW_DEPTH + 1} is "
        f"more than {MAX_WINDOW_DEPTH}"
    )


def test_a_negative_window():
    # The API schema refuses it first; an inline agent reaches only this.
    assert procedures_refusal([_good(window_depth=-1)]) == (
        "procedures[0] 'no_internet': window_depth -1 is less than 0"
    )


def test_two_procedures_with_one_name():
    assert procedures_refusal([_good(), _good()]) == (
        "procedures[1] 'no_internet': another procedure is already named 'no_internet'"
    )


def test_the_refusal_names_the_procedure_by_its_index():
    other = _procedure(
        [_step("ask", start=True, moves=_to("nowhere")), _step("end", "end")],
        name="billing",
    )

    assert procedures_refusal([_good(), other]).startswith(
        "procedures[1] 'billing': step 'ask' moves to 'nowhere'"
    )


def test_a_kind_or_start_mode_an_inline_agent_made_up():
    # The domain parses any string here; the API schema's Literal never
    # lets one through, so only an inline agent reaches this.
    bad_kind = _procedure(
        [_step("lights", "guess", start=True, moves=_to("done")), _step("done", "end")]
    )
    bad_start = _good(start="whenever")

    assert procedures_refusal([bad_kind]) == (
        "procedures[0] 'no_internet': step 'lights' has kind 'guess'; it must "
        "be one of ask, instruct, check, escalate, end"
    )
    assert procedures_refusal([bad_start]) == (
        "procedures[0] 'no_internet': start 'whenever' must be one of call, on_request"
    )


# --- an inline agent's entry that is not a procedure at all ------------------
#
# Its config arrives from call-init past any schema and is parsed as an
# `AgentConfig` before the build, where a raise would fail the call before it
# can say why. So the domain model keeps the entry, marked unreadable, and the
# rule names it (story 19): a bad call-init answer is visible, never a call
# silently running without its procedure.


def _inline(procedures: Any) -> list[Procedure]:
    from turncall.domain.models import AgentConfig

    return AgentConfig.model_validate(
        {"system_prompt": "hi", "procedures": procedures}
    ).procedures


def test_an_unreadable_entry_is_kept_for_the_rule_to_name():
    broken = {"name": "no_internet", "steps": [{"kind": "ask", "text": "Ask."}]}

    refusal = procedures_refusal(_inline([broken]))

    assert refusal is not None
    assert refusal.startswith("procedures[0] 'no_internet': cannot be read: ")
    assert "steps.0.id" in refusal


def test_an_entry_with_no_name_is_named_by_its_index():
    refusal = procedures_refusal(_inline([_good().model_dump(), "router"]))

    assert refusal is not None
    assert refusal.startswith("procedures[1]: cannot be read: ")


def test_procedures_that_are_not_a_list():
    assert procedures_refusal(_inline({"name": "no_internet"})) == (
        "procedures[0]: cannot be read: procedures must be a list"
    )


def test_a_sound_inline_procedure_reads_as_written():
    [one] = _inline([_good().model_dump()])

    assert one == _good()
    assert procedures_refusal([one]) is None
