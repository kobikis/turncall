"""What a move to an escalate step answers (#448): the move's result with the
action's beside it, and in an eval the action mocked as its tool would be —
it would act on a call that does not exist."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from turncall.domain.procedure_state import ProcedureState, move
from turncall.domain.procedures import Procedure
from turncall.orchestrator.procedure_escalation import escalation_of, run_escalation
from turncall.services.tool_mocks import ToolMocks

pytestmark = pytest.mark.unit

TRANSFER = {"target_number": "+15550009999", "transfer_mode": "cold"}


def _state() -> ProcedureState:
    procedure = Procedure.model_validate(
        {
            "name": "no_internet",
            "steps": [
                {
                    "id": "lights",
                    "kind": "ask",
                    "start": True,
                    "text": "Ask about the lights.",
                    "moves": [
                        {"to": "technician", "when": "none on"},
                        {"to": "done", "when": "fixed"},
                    ],
                },
                {"id": "done", "kind": "end", "text": "Close."},
                {
                    "id": "technician",
                    "kind": "escalate",
                    "text": "Say a person takes over.",
                    "escalate": {"transfer_call": TRANSFER},
                },
            ],
        }
    )
    return ProcedureState.at_call_start([procedure])


def _context(mocks: ToolMocks | None) -> Any:
    return SimpleNamespace(tool_mocks=mocks)


def test_only_a_move_onto_an_escalate_step_escalates():
    state = _state()
    assert escalation_of(move(state, "no_internet", "technician")) == (
        "transfer_call",
        TRANSFER,
    )
    assert escalation_of(move(state, "no_internet", "done")) is None
    assert escalation_of(move(state, "no_internet", "nowhere")) is None


async def test_a_move_elsewhere_answers_the_move_alone():
    moved = move(_state(), "no_internet", "done")
    assert await run_escalation(moved, _context(None), None) == moved.result


async def test_an_eval_mocks_the_action_and_the_move_carries_its_answer():
    mocks = ToolMocks(
        responses={"transfer_call": {"success": True, "message": "Transferring."}}
    )
    moved = move(_state(), "no_internet", "technician")

    result = json.loads(await run_escalation(moved, _context(mocks), None))

    assert result["success"] is True
    assert result["message"].startswith("Now at step 'technician' (escalate).")
    assert result["message"].endswith("Escalated with transfer_call: Transferring.")
    assert result["details"] == {
        "procedure": "no_internet",
        "step": "technician",
        "escalation": {
            "action": "transfer_call",
            "success": True,
            "message": "Transferring.",
        },
    }
    assert [c["tool_name"] for c in mocks.calls] == ["transfer_call"]


async def test_an_unmocked_action_in_an_eval_is_refused_like_its_tool():
    mocks = ToolMocks()
    moved = move(_state(), "no_internet", "technician")

    result = json.loads(await run_escalation(moved, _context(mocks), None))

    assert result["success"] is False
    assert mocks.refused == ["transfer_call"]
