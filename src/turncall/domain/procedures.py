"""Guided procedures (#417): a graph of steps an agent follows on a call.

The system holds the route and the model is shown a window of it each turn
(ADR-0032). These are the agent config's shapes, as a call runs them. They
are deliberately permissive: an inline agent's config arrives from a
call-init webhook past any API schema, and a graph that is wrong is the build
rule's to refuse with a reason (#444), never a parse error that fails the
call before it can say why. The API schema (`api/v1/schemas/procedures.py`)
is the strict one.

No pipecat here: the API schema imports this module.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from turncall.domain.enums import ProcedureStart, TransferMode

# The built-in that reports a move (#442). Offered by TurnCall to an agent
# with procedures, never listed in its `tools`.
MOVE_TO_STEP = "move_to_step"
# The built-ins an escalate step runs (#448).
TRANSFER_CALL = "transfer_call"
HANDOFF_TO_AGENT = "handoff_to_agent"

DEFAULT_WINDOW_DEPTH = 1


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProcedureMove(_Shape):
    """A move allowed from a step: to `to`, when `when` holds. The model
    judges the condition; TurnCall checks only that the move is allowed."""

    to: str
    when: str = ""


class BriefingFromSummary(_Shape):
    """A warm transfer's briefing written from the call so far, when the
    operator answers: `transfer_call`'s `{"from_summary": true}`."""

    from_summary: bool = True


class TransferEscalation(_Shape):
    """An escalate step's transfer: the `transfer_call` tool's fields (#448)."""

    target_number: str
    transfer_mode: str = TransferMode.COLD
    transfer_message: str | None = None
    # A literal text, or one written from the call's transcript.
    briefing: str | BriefingFromSummary | None = None
    fallback_message: str | None = None
    reason: str | None = None


class HandoffEscalation(_Shape):
    """An escalate step's handoff to another agent (#448)."""

    agent_id: str
    reason: str | None = None
    context: str | None = None


# An escalate step's action: the built-in it runs and that built-in's
# arguments, as the model would pass them.
EscalationAction = tuple[str, dict[str, Any]]


class StepEscalation(_Shape):
    """What an `escalate` step does: one of the two (#448)."""

    transfer_call: TransferEscalation | None = None
    handoff_to_agent: HandoffEscalation | None = None

    def actions(self) -> list[EscalationAction]:
        """Every action named, as `(tool, arguments)`: one, for a step the
        graph rule accepts."""
        named = (
            (TRANSFER_CALL, self.transfer_call),
            (HANDOFF_TO_AGENT, self.handoff_to_agent),
        )
        return [
            (tool, action.model_dump(exclude_none=True))
            for tool, action in named
            if action is not None
        ]

    def action(self) -> EscalationAction | None:
        """The step's one action, or None when it names none or both."""
        actions = self.actions()
        return actions[0] if len(actions) == 1 else None


class ProcedureStep(_Shape):
    id: str
    # A `StepKind`; any string parses, for the rule to name (#444).
    kind: str
    text: str = ""
    # The step the procedure starts at; exactly one per procedure (#444).
    start: bool = False
    # A tool the model should call at this step.
    tool: str | None = None
    moves: tuple[ProcedureMove, ...] = ()
    escalate: StepEscalation | None = None


class Procedure(_Shape):
    name: str
    # When it applies: what the model reads of an `on_request` one (#446).
    description: str = ""
    # A `ProcedureStart`; any string parses, for the rule to name (#444).
    start: str = ProcedureStart.CALL
    # How many steps ahead the window previews.
    window_depth: int = DEFAULT_WINDOW_DEPTH
    steps: tuple[ProcedureStep, ...] = Field(default_factory=tuple)
    # Why an inline agent's entry could not be read as a procedure at all,
    # set by `AgentConfig`'s validator in its place: the build fails the call
    # naming it (#444) rather than the call running without it.
    unreadable: str | None = None

    def step(self, step_id: str) -> ProcedureStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @property
    def start_step(self) -> ProcedureStep | None:
        return next((s for s in self.steps if s.start), None)


def read_procedure(entry: Any) -> Procedure:
    """An inline agent's procedures entry as a procedure, or, when it cannot
    be read as one at all, a procedure marked `unreadable` with why — for the
    build's rule to name (#444)."""
    try:
        return Procedure.model_validate(entry)
    except ValidationError as exc:
        name = entry.get("name") if isinstance(entry, dict) else None
        return Procedure(
            name=name if isinstance(name, str) else "",
            unreadable="; ".join(_problem(error) for error in exc.errors()),
        )


def _problem(error: Any) -> str:
    where = ".".join(str(part) for part in error["loc"])
    return f"{where}: {error['msg']}" if where else str(error["msg"])
