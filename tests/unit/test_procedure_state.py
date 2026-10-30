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
def test_a_procedure_not_running_is_refused():
    state = ProcedureState.at_call_start((NO_INTERNET, LATER))

    unknown = move(state, "no_such", "ask")
    idle = move(state, "billing", "ask")

    assert not unknown.accepted and not idle.accepted
    assert "no procedure named 'no_such'" in json.loads(unknown.result)["message"]
    assert "'billing' is not running" in json.loads(idle.result)["message"]
