"""An escalate step's action (#448): a procedure that cannot resolve the
problem ends safely.

A move that reaches an `escalate` step runs the step's action through the
path the tools take — a transfer is `transfer_call`'s, shielded as the tool
is, so it works on every carrier the tool works on and is refused the same
way where it is not; a handoff is `run_handoff`, the one the
`handoff_to_agent` tool and the live API run, which also ends the call's
procedures (`procedures.leave_procedures`). In an eval the action is mocked
as the tool would be: it acts on a call that does not exist.

The move's result carries the action's: the model learns where it is and
what became of the transfer or handoff in one answer.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from turncall.domain.procedure_state import MoveOutcome
from turncall.services.tool_mocks import intercept

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

TRANSFER_CALL = "transfer_call"
HANDOFF_TO_AGENT = "handoff_to_agent"


def escalation_of(outcome: MoveOutcome) -> tuple[str, dict[str, Any]] | None:
    """The tool and its arguments a move's escalate step runs, or None when
    the move reached no such step."""
    if not outcome.accepted or outcome.kind != "escalate":
        return None
    procedure = outcome.state.procedure(outcome.procedure)
    step = procedure.step(outcome.to_step) if procedure is not None else None
    action = step.escalate if step is not None else None
    if action is None:  # the graph rule refuses this at save and at build
        return None
    if action.transfer_call is not None:
        return TRANSFER_CALL, action.transfer_call.model_dump(exclude_none=True)
    if action.handoff_to_agent is not None:
        return HANDOFF_TO_AGENT, action.handoff_to_agent.model_dump(exclude_none=True)
    return None


async def run_escalation(
    outcome: MoveOutcome, call_context: CallContext, params: Any
) -> str:
    """The move's tool result, after running its escalate step's action when
    it reached one."""
    escalation = escalation_of(outcome)
    if escalation is None:
        return outcome.result
    name, args = escalation
    logger.info(
        "procedure_escalation: {procedure} at {step} runs {action}",
        procedure=outcome.procedure,
        step=outcome.to_step,
        action=name,
    )
    acted = intercept(call_context.tool_mocks, name, args)
    if acted is None:
        acted = await _act(name, args, call_context, params)
    return _with_action(outcome.result, name, acted)


async def _act(
    name: str, args: dict[str, Any], call_context: CallContext, params: Any
) -> str:
    from turncall.orchestrator.tool_bridge import _execute_builtin, run_handoff

    if name == TRANSFER_CALL:
        # Shielded as the tool is: the carrier closing the stream cancels it.
        return await asyncio.shield(_execute_builtin(name, args, call_context))
    return (await run_handoff(args, call_context, params)).result


def _with_action(moved: str, name: str, acted: str) -> str:
    """The move's result with the action's beside it: `success` is the
    action's, since the move itself was accepted either way."""
    move = json.loads(moved)
    try:
        action = json.loads(acted)
    except ValueError:
        action = {"message": acted}
    if not isinstance(action, dict):
        action = {"message": str(action)}
    # A built-in answers `{success, message}`; an error (an eval refusing an
    # unmocked action, say) answers `{error}`.
    succeeded = action.get("success") is not False and "error" not in action
    said = action.get("message") or action.get("error") or "Done."
    return json.dumps(
        {
            "success": succeeded,
            "message": f"{move['message']} Escalated with {name}: {said}",
            "details": {**move["details"], "escalation": {"action": name, **action}},
        }
    )
