"""A call's procedures (#442, ADR-0032): the system holds the route, the
model sees a window.

- `CallProcedures` is the call's holder: one immutable `ProcedureState`,
  replaced whole by a move. Every `CallContext` starts with an empty one.
- `with_procedures` is the build's config transform: an agent with
  procedures, on a cascade call, gets `move_to_step` and a running state.
- `run_move_to_step` is the tool: a move is checked and applied with nothing
  awaited in between, so a barge-in cancelling the tool cannot leave the
  state half moved. Each move is recorded on the call's event log (#443),
  in the order made, off the tool's path.
- `ProcedureWindowProcessor` sits between the user aggregator and the LLM and
  puts this turn's window right before the caller's turn, replacing the
  previous turn's (`turn_message.replace_turn_message`). One processor in the
  cascade pipeline, so every carrier and WebRTC get the same window.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from turncall.domain.enums import CallEventType
from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.domain.procedure_state import MoveOutcome, ProcedureState, move
from turncall.domain.procedure_window import WINDOW_MARKER, startable_section
from turncall.domain.procedures import MOVE_TO_STEP, Procedure
from turncall.orchestrator.turn_message import replace_turn_message
from turncall.orchestrator.turn_metrics import store_call_event

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext


# Event writes still running, kept referenced: asyncio holds only a weak one.
_WRITING: set[asyncio.Task[None]] = set()


class CallProcedures:
    """The call's place in its procedures. Shared by the window processor and
    the tool through the call's context; a move puts a new state in place of
    the old in one assignment."""

    def __init__(self, state: ProcedureState | None = None) -> None:
        self.state = state or ProcedureState()
        self._last_write: asyncio.Task[None] | None = None

    def record(self, write: Coroutine[Any, Any, None]) -> None:
        """Run `write` in the background once the previous one is done, so
        the call's log keeps the moves in the order they were made: two moves
        in one response run concurrently, and either write may be slower."""
        previous = self._last_write

        async def in_turn() -> None:
            if previous is not None:
                await asyncio.wait({previous})
            await write

        task = asyncio.create_task(in_turn())
        _WRITING.add(task)
        task.add_done_callback(_WRITING.discard)
        self._last_write = task


def no_procedures() -> CallProcedures:
    return CallProcedures()


def with_procedures(
    config: AgentConfig, call_context: CallContext
) -> tuple[AgentConfig, CallContext]:
    """The config the call runs with its procedures' tool, and the context
    holding them running — or both as given, for an agent with none or an
    S2S call (no window can reach a realtime model; #444 warns of it)."""
    if config.pipeline_mode == "s2s" or not config.procedures:
        return config, call_context
    tools = [
        move_to_step_tool(config.procedures),
        # An inline agent skipped the API's refusal of the name.
        *(t for t in config.tools if t.name != MOVE_TO_STEP),
    ]
    holder = CallProcedures(ProcedureState.at_call_start(config.procedures))
    logger.info(
        "procedures_running: {names}", names=[r.name for r in holder.state.runs]
    )
    update: dict[str, Any] = {"tools": tools}
    if (section := startable_section(config.procedures)) is not None:
        update["system_prompt"] = f"{config.system_prompt}\n\n{section}"
    return (
        config.model_copy(update=update),
        replace(call_context, procedures=holder),
    )


def move_to_step_tool(procedures: Sequence[Procedure]) -> ToolDefinition:
    """The built-in that reports a move in one of `procedures`."""
    return ToolDefinition(
        name=MOVE_TO_STEP,
        description=(
            "Report a move in a procedure, as its window allows: to one of the "
            "allowed moves when its condition holds, or back to the previous "
            "step. A procedure under 'Procedures you can start' is started by a "
            "move to its start step. A move that is not allowed is refused "
            "with the reason."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "procedure": {
                    "type": "string",
                    "description": "The procedure's name",
                    "enum": [p.name for p in procedures],
                },
                "to": {
                    "type": "string",
                    "description": "The id of the step to move to",
                },
            },
            "required": ["procedure", "to"],
        },
    )


def run_move_to_step(args: dict[str, Any], call_context: CallContext) -> MoveOutcome:
    """Check and apply one reported move; its `result` is the tool's."""
    holder: CallProcedures = call_context.procedures
    outcome = move(
        holder.state,
        str(args.get("procedure") or "").strip(),
        str(args.get("to") or "").strip(),
    )
    holder.state = outcome.state
    logger.info(
        "procedure_move: {procedure} {at} -> {to} {verdict}",
        procedure=outcome.procedure,
        at=outcome.from_step,
        to=outcome.to_step,
        verdict="accepted" if outcome.accepted else "refused",
    )
    _record_move(call_context, outcome)
    return outcome


def _record_move(call_context: CallContext, outcome: MoveOutcome) -> None:
    """A `procedure.step` `{procedure, from, to, kind}` for an accepted move
    (`kind` the step reached, which the call's `procedures` summary reads), a
    `procedure.refused` `{procedure, from, to}` for a refused one. Stored
    only, identifiers only; nothing for a call with no row (an eval)."""
    if not call_context.records:
        return
    payload: dict[str, Any] = {
        "procedure": outcome.procedure,
        "from": outcome.from_step,
        "to": outcome.to_step,
    }
    if outcome.accepted:
        event_type = CallEventType.PROCEDURE_STEP
        payload["kind"] = outcome.kind
    else:
        event_type = CallEventType.PROCEDURE_REFUSED
    call_context.procedures.record(store_call_event(call_context, event_type, payload))


class ProcedureWindowProcessor(FrameProcessor):
    """Puts this turn's procedure window right before the caller's turn.

    pipecat's user aggregator hands each turn on as an `LLMContextFrame`,
    downstream; that is what this acts on, as the KB auto processor does.
    With nothing running it touches nothing — unless it placed a window
    before, which then has to come out.
    """

    def __init__(self, procedures: CallProcedures, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._procedures = procedures
        self._placed = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, LLMContextFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._place_window(frame.context)
        await self.push_frame(frame, direction)

    async def _place_window(self, context: Any) -> None:
        text = self._procedures.state.window_text()
        if text is None and not self._placed:
            return
        self._placed = text is not None

        async def window(_messages: list[Any], _turn: str) -> dict[str, Any] | None:
            return {"role": "developer", "content": text} if text else None

        await replace_turn_message(context, WINDOW_MARKER, window)


def procedure_window_stage(call_context: CallContext) -> list[FrameProcessor]:
    """The window processor, in every cascade pipeline: a handoff can bring
    procedures to a call that started with none (#448)."""
    return [ProcedureWindowProcessor(call_context.procedures)]
