"""An agent's procedures at the API (#442): the strict shape.

The domain's (`domain/procedures.py`) is permissive, for an inline agent that
arrives past this schema. Only the shape is checked here; the graph rule —
ids, the start, every move's target, reachability, an ending, the caps — is
#444's, applied here and at build.
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from turncall.api.v1.schemas import E164_PATTERN
from turncall.api.v1.schemas.call_control import BriefingSummary
from turncall.domain.enums import ProcedureStart, StepKind, TransferMode
from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    PROCEDURE_NAME_MAX,
    PROCEDURE_NAME_PATTERN,
)
from turncall.domain.procedure_graph import procedures_refusal
from turncall.domain.procedures import DEFAULT_WINDOW_DEPTH, MOVE_TO_STEP, Procedure
from turncall.services.retrieval import KNOWLEDGE_TOOL_SCHEMA

STEP_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
STEP_ID_MAX = 64

PROCEDURES_DESCRIPTION = (
    "Graphs of steps the agent follows (#417): the model is shown a window of "
    "the current step each turn and reports each move with move_to_step. "
    "Cascade voice only"
)


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
    transfer_mode: TransferMode = TransferMode.COLD
    transfer_message: str | None = Field(default=None, max_length=2000)
    briefing: Annotated[str, Field(max_length=4000)] | BriefingSummary | None = Field(
        default=None,
        description=(
            'Spoken to the operator on a warm transfer: a text, or {"from_summary": '
            "true} for a summary of the call so far"
        ),
    )
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
    kind: StepKind = Field(
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
        max_length=PROCEDURE_NAME_MAX,
        pattern=PROCEDURE_NAME_PATTERN,
        description="How the model names the procedure when it reports a move",
    )
    description: str = Field(
        default="", max_length=2000, description="When the procedure applies"
    )
    start: ProcedureStart = Field(
        default=ProcedureStart.CALL,
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


def domain_procedures(procedures: Sequence[ProcedureSchema]) -> list[Procedure]:
    """The procedures as a call runs them."""
    return [Procedure.model_validate(p.model_dump()) for p in procedures]


def procedures_refusal_at_api(
    procedures: Sequence[ProcedureSchema],
    *,
    tool_names: Sequence[str],
    has_mcp_servers: bool,
) -> str | None:
    """What is wrong with an agent's procedures at save, or None.

    `move_to_step` is offered by the platform to an agent with procedures
    (#442), built from them as `load_skill` is from skills, so it is never
    listed. Then the graph rule, run here and at build for an inline agent
    (#444); here it also checks each step's tool is one the agent can call
    (#447), unless the agent has an MCP server, whose tools are discovered
    at call start. `query_knowledge` comes from KB links the config does not
    hold.
    """
    if MOVE_TO_STEP in tool_names:
        return (
            f"tools: '{MOVE_TO_STEP}' is offered automatically to an agent "
            "with procedures; define them in `procedures` instead"
        )
    callable_tools = (
        None
        if has_mcp_servers
        else {*tool_names, *BUILTIN_TOOL_NAMES, KNOWLEDGE_TOOL_SCHEMA["name"]}
    )
    return procedures_refusal(
        domain_procedures(procedures), callable_tools=callable_tools
    )
