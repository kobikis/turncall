"""The procedure window (#442): what the model is shown of a procedure on one
turn — the current step, its moves, the move back, and a preview up to the
procedure's depth. A pure function of the graph, the path and the depth."""

import pytest

from turncall.domain.procedure_window import build_window, startable_section
from turncall.domain.procedures import Procedure

NO_INTERNET = Procedure.model_validate(
    {
        "name": "no_internet",
        "description": "Caller has no internet connection at home",
        "start": "call",
        "window_depth": 1,
        "steps": [
            {
                "id": "lights",
                "kind": "ask",
                "start": True,
                "text": "Ask which lights on the router are on.",
                "moves": [
                    {"to": "power", "when": "the power light is off"},
                    {"to": "restart", "when": "the internet light is red"},
                ],
            },
            {
                "id": "power",
                "kind": "check",
                "text": "Have the caller check the power cable.",
                "moves": [{"to": "technician", "when": "still no power light"}],
            },
            {
                "id": "restart",
                "kind": "instruct",
                "text": "Have the caller restart the router and wait 2 minutes.",
                "moves": [
                    {
                        "to": "after_restart",
                        "when": "the caller confirms they waited 2 minutes",
                    }
                ],
            },
            {
                "id": "after_restart",
                "kind": "check",
                "tool": "line_status",
                "text": "Check the line status.",
                "moves": [
                    {"to": "resolved", "when": "the internet light is green"},
                    {"to": "technician", "when": "the light is still red"},
                ],
            },
            {"id": "resolved", "kind": "end", "text": "Close politely."},
            {
                "id": "technician",
                "kind": "escalate",
                "text": "Explain a technician will be booked.",
                "escalate": {"transfer_call": {"target_number": "+15550009999"}},
            },
        ],
    }
)


@pytest.mark.unit
def test_the_window_shows_the_current_step_its_moves_and_one_step_ahead():
    window = build_window(NO_INTERNET, ("lights", "restart"), depth=1)

    assert window == (
        'Procedure "no_internet" — current step: restart (instruct)\n'
        "  Do now: Have the caller restart the router and wait 2 minutes.\n"
        "  Allowed moves:\n"
        "    → after_restart  when: the caller confirms they waited 2 minutes\n"
        "    ← back to lights  (always allowed)\n"
        "  Next steps (preview): what comes next, not what to do now. Do a "
        "step's work (ask its question, call its tool) only in a reply that "
        "also moves there.\n"
        "    after_restart (check, tool: line_status): Check the line status.\n"
        "  Report every move with move_to_step. Do not skip a step. If the "
        "caller's answer does not fit any move, ask again."
    )


@pytest.mark.unit
def test_the_preview_never_reaches_past_the_depth():
    one = build_window(NO_INTERNET, ("lights",), depth=1)
    two = build_window(NO_INTERNET, ("lights",), depth=2)
    none = build_window(NO_INTERNET, ("lights",), depth=0)

    # One ahead: power and restart, nothing they lead to.
    assert "power (check)" in one and "restart (instruct)" in one
    for beyond in ("after_restart", "technician", "resolved"):
        assert beyond not in one
    # Two ahead: what power and restart lead to, said where from.
    assert "technician (escalate): Explain a technician will be booked." in two
    assert "— after power" in two
    assert "after_restart (check, tool: line_status)" in two
    assert "resolved" not in two
    # None ahead: the moves still name their targets, but no preview.
    assert "preview" not in none
    assert "Have the caller" not in none


@pytest.mark.unit
def test_the_start_step_has_no_move_back():
    window = build_window(NO_INTERNET, ("lights",), depth=1)

    assert "back to" not in window
    assert "→ power  when: the power light is off" in window


@pytest.mark.unit
def test_a_step_with_no_way_on_says_so():
    window = build_window(
        NO_INTERNET, ("lights", "restart", "after_restart", "resolved"), depth=1
    )

    assert "current step: resolved (end)" in window
    assert "← back to after_restart  (always allowed)" in window
    assert "preview" not in window


@pytest.mark.unit
def test_a_step_that_can_escalate_says_when_to():
    at_power = build_window(NO_INTERNET, ("lights", "power"), depth=1)
    at_check = build_window(
        NO_INTERNET, ("lights", "restart", "after_restart"), depth=1
    )
    at_lights = build_window(NO_INTERNET, ("lights",), depth=1)

    escalate = (
        "  If the procedure cannot resolve the caller's problem, escalate: "
        "move to technician."
    )
    assert escalate in at_power.splitlines()
    assert escalate in at_check.splitlines()
    # No escalate step is one move away: nothing to say.
    assert "escalate:" not in at_lights


@pytest.mark.unit
def test_only_on_request_procedures_are_listed_to_start_and_never_a_step():
    later = NO_INTERNET.model_copy(update={"name": "later", "start": "on_request"})

    section = startable_section((NO_INTERNET, later))

    assert section is not None
    assert section.startswith("## Procedures you can start\n")
    assert section.endswith(
        "\n- later: Caller has no internet connection at home (start step: lights)"
    )
    assert "no_internet" not in section
    assert all(step.text not in section for step in NO_INTERNET.steps)
    # A `call` procedure is the window's: the prompt gains nothing for it.
    assert startable_section((NO_INTERNET,)) is None
