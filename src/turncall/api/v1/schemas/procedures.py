"""An agent's procedures at the API (#442): the strict shape.

The domain's (`domain/procedures.py`) is permissive, for an inline agent that
arrives past this schema. Only the shape is checked here; the graph rule —
ids, the start, every move's target, reachability, an ending, the caps — is
#444's, applied here and at build.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from turncall.api.v1.schemas import E164_PATTERN
from turncall.domain.models import SKILL_NAME_MAX, SKILL_NAME_PATTERN
from turncall.domain.procedures import DEFAULT_WINDOW_DEPTH

STEP_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
STEP_ID_MAX = 64


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcedureMoveSchema(_Strict):
    to: str = Field(
        ...,
        min_length=1,
        max_length=STEP_ID_MAX,
        pattern=STEP_ID_PATTERN,
        description="The step this move goes to",
    )
    when: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The condition, in plain language; the model judges it",
    )


class TransferEscalationSchema(_Strict):
    """The `transfer_call` tool's fields."""

    target_number: str = Field(..., pattern=E164_PATTERN)
    transfer_mode: Literal["cold", "warm"] = "cold"
    transfer_message: str | None = Field(default=None, max_length=2000)
    briefing: str | None = Field(default=None, max_length=4000)
    fallback_message: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=500)


class HandoffEscalationSchema(_Strict):
    agent_id: str = Field(..., min_length=1, description="The agent to hand off to")
    reason: str | None = Field(default=None, max_length=500)
    context: str | None = Field(default=None, max_length=4000)


class StepEscalationSchema(_Strict):
    """What an `escalate` step does: a transfer or a handoff."""

    transfer_call: TransferEscalationSchema | None = None
    handoff_to_agent: HandoffEscalationSchema | None = None


class ProcedureStepSchema(_Strict):
    id: str = Field(..., min_length=1, max_length=STEP_ID_MAX, pattern=STEP_ID_PATTERN)
    kind: Literal["ask", "instruct", "check", "escalate", "end"] = Field(
        ...,
        description=(
            "ask a question, instruct the caller, check something (a tool may "
            "help), escalate (transfer or hand off), or end the procedure"
        ),
    )
    text: str = Field(
        ..., min_length=1, max_length=4000, description="What to do at this step"
    )
    start: bool = Field(
        default=False, description="The step the procedure starts at; one per procedure"
    )
    tool: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="A tool the model should call at this step",
    )
    moves: list[ProcedureMoveSchema] = Field(
        default_factory=list,
        description=(
            "The moves allowed from this step. Moving back to the previous "
            "step is always allowed and needs none"
        ),
    )
    escalate: StepEscalationSchema | None = Field(
        default=None, description="What an escalate step does"
    )


class ProcedureSchema(_Strict):
    name: str = Field(
        ...,
        min_length=1,
        max_length=SKILL_NAME_MAX,
        pattern=SKILL_NAME_PATTERN,
        description="How the model names the procedure when it reports a move",
    )
    description: str = Field(
        default="", max_length=2000, description="When the procedure applies"
    )
    start: Literal["call", "on_request"] = Field(
        default="call",
        description=(
            "call: running from the first turn. on_request: the model starts "
            "it when it applies"
        ),
    )
    window_depth: int = Field(
        default=DEFAULT_WINDOW_DEPTH,
        ge=0,
        description="How many steps ahead of the current one the model is shown",
    )
    steps: list[ProcedureStepSchema] = Field(..., min_length=1)
