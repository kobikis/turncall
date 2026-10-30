"""A call's place in its procedures (#442): where each one is, and what a
reported move does to that. Pure: a move answers a new state and the model's
tool result, and a refused move answers the state it was given."""

import json

import pytest

from tests.unit.test_procedure_window import NO_INTERNET
from turncall.domain.procedure_state import ProcedureState, move
from turncall.domain.procedures import Procedure

LATER = Procedure.model_validate(
    {
        "name": "billing",
        "start": "on_request",
        "steps": [{"id": "ask", "kind": "end", "start": True, "text": "Ask."}],
    }
)


LATER_TWO_STEPS = Procedure.model_validate(
    {
        "name": "billing",
        "start": "on_request",
        "steps": [
            {
                "id": "ask",
                "kind": "ask",
                "start": True,
                "text": "Ask.",
                "moves": [{"to": "refund", "when": "a refund is due"}],
            },
            {"id": "refund", "kind": "end", "text": "Refund."},
        ],
    }
)


def _at(*path: str) -> ProcedureState:
    state = ProcedureState.at_call_start((NO_INTERNET,))
    for step in path[1:]:
        outcome = move(state, "no_internet", step)
        assert outcome.accepted, outcome.result
        state = outcome.state
    return state


@pytest.mark.unit
def test_a_call_procedure_starts_at_its_start_step():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER))

    assert [(r.name, r.path) for r in state.runs] == [("no_internet", ("lights",))]
    assert "current step: lights (ask)" in state.window_text()
    assert state.window_text().startswith("[Procedure]")


@pytest.mark.unit
def test_no_procedures_no_window():
    assert ProcedureState.at_call_start(()).window_text() is None


@pytest.mark.unit
def test_an_allowed_move_moves_and_says_what_to_do_there():
    state = _at("lights")

    outcome = move(state, "no_internet", "restart")

    assert outcome.accepted
    assert outcome.state.run("no_internet").path == ("lights", "restart")
    assert (outcome.from_step, outcome.to_step, outcome.kind) == (
        "lights",
        "restart",
        "instruct",
    )
    result = json.loads(outcome.result)
    assert result["success"] is True
    assert "Have the caller restart the router and wait 2 minutes." in result["message"]
    # The state it was given is untouched.
    assert state.run("no_internet").path == ("lights",)


@pytest.mark.unit
def test_a_move_not_allowed_is_refused_naming_the_allowed_moves():
    state = _at("lights", "restart")

    outcome = move(state, "no_internet", "resolved")

    assert not outcome.accepted
    assert outcome.state is state
    result = json.loads(outcome.result)
    assert result["success"] is False
    message = result["message"]
    assert "'resolved' is not an allowed move from 'restart'" in message
    assert "after_restart (when the caller confirms they waited 2 minutes)" in message
    assert "back to lights" in message
    assert result["details"]["allowed"] == ["after_restart", "lights"]


@pytest.mark.unit
def test_a_step_that_does_not_exist_is_refused_as_such():
    outcome = move(_at("lights"), "no_internet", "router")

    assert not outcome.accepted
    assert "'no_internet' has no step 'router'" in json.loads(outcome.result)["message"]


@pytest.mark.unit
def test_moving_back_needs_no_move_entry():
    state = _at("lights", "restart")

    outcome = move(state, "no_internet", "lights")

    assert outcome.accepted
    assert outcome.state.run("no_internet").path == ("lights",)
    assert "current step: lights" in outcome.state.window_text()


@pytest.mark.unit
def test_an_on_request_procedure_starts_at_its_start_step_on_a_move_there():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER_TWO_STEPS))

    outcome = move(state, "billing", "ask")

    assert outcome.accepted
    assert (outcome.from_step, outcome.to_step, outcome.kind) == (None, "ask", "ask")
    assert outcome.state.run("billing").path == ("ask",)
    # The procedure already running keeps its place.
    assert outcome.state.run("no_internet").path == ("lights",)
    result = json.loads(outcome.result)
    assert result["success"] is True
    assert "Started 'billing'" in result["message"]
    assert "Do now: Ask." in result["message"]
    assert result["details"] == {"procedure": "billing", "step": "ask"}


@pytest.mark.unit
def test_a_procedure_not_running_is_refused_unless_started_at_its_start():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER_TWO_STEPS))

    unknown = move(state, "no_such", "ask")
    mid_way = move(state, "billing", "refund")

    assert not unknown.accepted and not mid_way.accepted
    assert mid_way.state is state
    assert "no procedure named 'no_such'" in json.loads(unknown.result)["message"]
    refused = json.loads(mid_way.result)
    assert refused["message"] == (
        "'billing' is not running. Start it at its start step 'ask'. "
        "Running: ['no_internet']."
    )
    assert refused["details"] == {
        "procedure": "billing",
        "at": None,
        "allowed": ["ask"],
    }


@pytest.mark.unit
def test_a_call_procedure_not_running_cannot_be_started_by_a_move():
    # Not running: e.g. left on a handoff (#448). Only `on_request` starts.
    state = ProcedureState(procedures=(NO_INTERNET,))

    outcome = move(state, "no_internet", "lights")

    assert not outcome.accepted
    refused = json.loads(outcome.result)
    assert refused["message"] == "'no_internet' is not running. Running: []."
    assert refused["details"]["allowed"] == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "ending"),
    [
        (("lights", "restart", "after_restart"), "resolved"),
        (("lights", "power"), "technician"),
    ],
)
def test_reaching_an_end_or_escalate_step_finishes_the_procedure(path, ending):
    outcome = move(_at(*path), "no_internet", ending)

    assert outcome.accepted
    assert outcome.state.run("no_internet") is None
    assert outcome.state.finished_run("no_internet").path == (*path, ending)
    # Its window stops.
    assert outcome.state.window_text() is None
    assert "finished" in json.loads(outcome.result)["message"]


@pytest.mark.unit
def test_a_finished_procedure_refuses_every_move_back_included():
    finished = move(_at("lights", "power"), "no_internet", "technician").state

    back = move(finished, "no_internet", "power")
    on = move(finished, "no_internet", "resolved")

    for outcome in (back, on):
        assert not outcome.accepted
        assert outcome.state is finished
        refused = json.loads(outcome.result)
        assert refused["message"] == (
            "'no_internet' is finished: it ended at step 'technician'. Running: []."
        )
        assert refused["details"] == {
            "procedure": "no_internet",
            "at": "technician",
            "allowed": [],
        }


@pytest.mark.unit
def test_a_finished_on_request_procedure_is_not_started_again():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER_TWO_STEPS))
    state = move(state, "billing", "ask").state
    finished = move(state, "billing", "refund").state

    again = move(finished, "billing", "ask")

    assert finished.run("billing") is None
    assert not again.accepted
    assert "'billing' is finished" in json.loads(again.result)["message"]
    # The other procedure is untouched.
    assert finished.run("no_internet").path == ("lights",)


@pytest.mark.unit
def test_a_procedure_started_at_an_ending_step_is_finished_at_once():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER))

    outcome = move(state, "billing", "ask")

    assert outcome.accepted and outcome.kind == "end"
    assert outcome.state.run("billing") is None
    assert outcome.state.finished_run("billing").path == ("ask",)
