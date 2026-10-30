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


_TRANSFER = {"transfer_call": {"target_number": "+15550009999"}}
_HANDOFF = {"handoff_to_agent": {"agent_id": "7b0c8d4e-1f2a-4b3c-9d8e-6f5a4b3c2d1e"}}


def _good(**extra: Any) -> Procedure:
    return _procedure(
        [
            _step("lights", start=True, moves=_to("restart", "technician")),
            _step("restart", "instruct", moves=_to("done", "technician")),
            _step("technician", "escalate", escalate=_TRANSFER),
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


def test_a_step_tool_the_agent_cannot_call():
    # #447: checked only when the caller knows what the agent can call (the
    # API, for an agent with no MCP server).
    with_tool = _procedure(
        [
            _step("lights", "check", start=True, tool="line_stats", moves=_to("done")),
            _step("done", "end"),
        ]
    )

    assert procedures_refusal([with_tool]) is None
    assert procedures_refusal([with_tool], callable_tools={"line_stats"}) is None
    assert procedures_refusal([with_tool], callable_tools={"line_status"}) == (
        "procedures[0] 'no_internet': step 'lights' names tool 'line_stats', "
        "which is not one of the agent's tools or a built-in"
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


# --- an escalate step's action (#448) ---------------------------------------
#
# An escalate step names what it does, and reaching it does that: a transfer
# with the `transfer_call` tool's fields, or a handoff to an agent. A step
# that names neither, or both, would leave the call where it is with the
# procedure over; one that names an action but is not an escalate step would
# never run it.


def _escalating(action: Any) -> Procedure:
    extra = {} if action is None else {"escalate": action}
    return _procedure(
        [
            _step("lights", start=True, moves=_to("technician")),
            _step("technician", "escalate", **extra),
        ]
    )


def test_an_escalate_step_transfers_or_hands_off():
    assert procedures_refusal([_escalating(_TRANSFER)]) is None
    assert procedures_refusal([_escalating(_HANDOFF)]) is None


def test_an_escalate_step_with_no_action():
    expected = (
        "procedures[0] 'no_internet': step 'technician' escalates with no "
        "action; give it escalate.transfer_call or escalate.handoff_to_agent"
    )

    assert procedures_refusal([_escalating(None)]) == expected
    assert procedures_refusal([_escalating({})]) == expected


def test_an_escalate_step_with_both_actions():
    assert procedures_refusal([_escalating({**_TRANSFER, **_HANDOFF})]) == (
        "procedures[0] 'no_internet': step 'technician' names both a transfer "
        "and a handoff; an escalate step does one"
    )


def test_an_action_on_a_step_that_does_not_escalate():
    bad = _procedure(
        [
            _step("lights", start=True, moves=_to("done"), escalate=_TRANSFER),
            _step("done", "end"),
        ]
    )

    assert procedures_refusal([bad]) == (
        "procedures[0] 'no_internet': step 'lights' is of kind 'ask'; only an "
        "escalate step takes an escalate action"
    )


def test_an_escalation_an_inline_agent_got_wrong():
    # The API schema refuses these by shape; an inline agent's config parses
    # any string, and a handoff then has no agent to go to.
    bad_agent = _escalating({"handoff_to_agent": {"agent_id": "the-billing-one"}})
    bad_mode = _escalating(
        {"transfer_call": {"target_number": "+15550009999", "transfer_mode": "hot"}}
    )
    bad_number = _escalating({"transfer_call": {"target_number": "the desk"}})

    assert procedures_refusal([bad_agent]) == (
        "procedures[0] 'no_internet': step 'technician' hands off to "
        "'the-billing-one', which is not an agent id"
    )
    assert procedures_refusal([bad_mode]) == (
        "procedures[0] 'no_internet': step 'technician' transfers with mode "
        "'hot'; it must be one of cold, warm"
    )
    assert procedures_refusal([bad_number]) == (
        "procedures[0] 'no_internet': step 'technician' transfers to "
        "'the desk', which is not an E.164 number"
    )
