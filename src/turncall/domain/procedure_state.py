"""A call's place in its procedures (#442, ADR-0032), and what a move does.

`ProcedureState` is immutable: a move answers a new one, which the call's
holder (`orchestrator/procedures.CallProcedures`) puts in place of the old in
one assignment. A refused move answers the state it was given. No pipecat,
no I/O.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from turncall.domain.enums import ENDING_STEP_KINDS, ProcedureStart, StepKind
from turncall.domain.procedure_window import build_window, steps_ahead, window_message
from turncall.domain.procedures import MOVE_TO_STEP, Procedure, ProcedureStep


@dataclass(frozen=True)
class ProcedureRun:
    """One running procedure: the steps it came by, the current one last.
    A move back takes the current step off."""

    name: str
    path: tuple[str, ...]

    @property
    def current(self) -> str:
        return self.path[-1]


@dataclass(frozen=True)
class ProcedureState:
    """The agent's procedures, those of them running, in the order started,
    and those finished: a procedure that reaches an `end` or `escalate` step
    stops running there, for good."""

    procedures: tuple[Procedure, ...] = ()
    runs: tuple[ProcedureRun, ...] = ()
    finished: tuple[ProcedureRun, ...] = ()

    @classmethod
    def at_call_start(cls, procedures: Sequence[Procedure]) -> "ProcedureState":
        """Every `start: "call"` procedure started, at its start step."""
        state = cls(procedures=tuple(procedures))
        for procedure in procedures:
            start = procedure.start_step
            if procedure.start == ProcedureStart.CALL and start is not None:
                state = state._placed(ProcedureRun(procedure.name, (start.id,)))
        return state

    def procedure(self, name: str) -> Procedure | None:
        return next((p for p in self.procedures if p.name == name), None)

    def run(self, name: str) -> ProcedureRun | None:
        return next((r for r in self.runs if r.name == name), None)

    def finished_run(self, name: str) -> ProcedureRun | None:
        return next((r for r in self.finished if r.name == name), None)

    def started(self) -> tuple[ProcedureRun, ...]:
        """Every procedure started, running or finished: at a call's start,
        each at its start step."""
        return (*self.runs, *self.finished)

    def window_text(self) -> str | None:
        """This turn's window message, or None with nothing running."""
        windows = [
            build_window(procedure, run.path, depth=procedure.window_depth)
            for run in self.runs
            if (procedure := self.procedure(run.name)) is not None
        ]
        return window_message(windows) if windows else None

    def step_tool_followup(self, tool: str) -> str | None:
        """What a running procedure adds to the result of its current step's
        own tool, or of a step one move ahead: report the move now. The
        inference reading a tool result gets no window, and the move into the
        step that named its moves can be several replies back by then — call
        c02c84fd loaded a skill, booked, and ended the call from `book`
        without moving to `confirmed`; call 7d351817 booked from `invite`,
        never moving to `book`. None for any other tool."""
        lines = []
        for run in self.runs:
            placed = self._procedure_and_step(run)
            line = _tool_followup(run, *placed, tool) if placed else None
            if line:
                lines.append(line)
        return "\n".join(lines) or None

    def current_step(self, run: ProcedureRun) -> ProcedureStep | None:
        placed = self._procedure_and_step(run)
        return placed[1] if placed else None

    def _procedure_and_step(
        self, run: ProcedureRun
    ) -> tuple[Procedure, ProcedureStep] | None:
        """`run`'s procedure and the step it is at; None when either is gone."""
        procedure = self.procedure(run.name)
        if procedure is None or (step := procedure.step(run.current)) is None:
            return None
        return procedure, step

    def _placed(self, run: ProcedureRun) -> "ProcedureState":
        """The state with `run` where it now stands: running in its place
        (or last, newly started), or finished at an `end` or `escalate`
        step."""
        others = tuple(r for r in self.runs if r.name != run.name)
        if self._ends_at(run):
            return replace(self, runs=others, finished=(*self.finished, run))
        if self.run(run.name) is None:
            return replace(self, runs=(*self.runs, run))
        runs = tuple(run if r.name == run.name else r for r in self.runs)
        return replace(self, runs=runs)

    def _ends_at(self, run: ProcedureRun) -> bool:
        step = self.current_step(run)
        return step is not None and step.kind in ENDING_STEP_KINDS


@dataclass(frozen=True)
class MoveOutcome:
    """What a reported move did: the state after it (the same one when
    refused) and the tool result the model reads."""

    accepted: bool
    state: ProcedureState
    procedure: str
    # None when the move started the procedure (#446).
    from_step: str | None
    to_step: str
    # The target step's kind, for an accepted move.
    kind: str | None
    result: str
    # A move to the current step: the model staying there. Neither accepted
    # nor refused, so neither event is recorded.
    stayed: bool = False


def move(state: ProcedureState, procedure: str, to: str) -> MoveOutcome:
    """Move `procedure` to step `to`: one of the current step's moves, or
    back to the previous step — or, for an `on_request` procedure not yet
    running, its start step, which starts it. Anything else is refused,
    naming what is allowed. A finished procedure takes no move at all: what
    it reached stands."""
    run = state.run(procedure)
    graph = state.procedure(procedure)
    if (done := state.finished_run(procedure)) is not None:
        names = [r.name for r in state.runs]
        message = (
            f"'{procedure}' is finished: it ended at step '{done.current}'. "
            f"Running: {names}."
        )
        return _refused(state, procedure, done.current, to, message)
    if graph is not None and run is None:
        return _start(state, graph, to)
    if graph is None or run is None:
        names = [r.name for r in state.runs]
        message = f"There is no procedure named '{procedure}'. Running: {names}."
        return _refused(state, procedure, None, to, message)
    current = graph.step(run.current)
    target = graph.step(to)
    back = run.path[-2] if len(run.path) > 1 else None
    if current is None or target is None:
        message = f"'{procedure}' has no step '{to}'. {_allowed_text(current, back)}"
        return _refused(state, procedure, run.current, to, message, current, back)
    if to == run.current:
        return _stayed(state, procedure, current, back)
    if to == back:
        moved = replace(run, path=run.path[:-1])
    elif any(m.to == to for m in current.moves):
        moved = replace(run, path=(*run.path, to))
    else:
        message = (
            f"'{to}' is not an allowed move from '{current.id}'. "
            f"{_allowed_text(current, back)}"
        )
        return _refused(state, procedure, run.current, to, message, current, back)
    return MoveOutcome(
        accepted=True,
        state=state._placed(moved),
        procedure=procedure,
        from_step=run.current,
        to_step=to,
        kind=target.kind,
        result=_result(
            True,
            _arrived(target, _previous(moved)),
            {"procedure": procedure, "step": to},
        ),
    )


def _tool_followup(
    run: ProcedureRun, procedure: Procedure, step: ProcedureStep, tool: str
) -> str | None:
    at = f"[Procedure] '{run.name}' is at step '{step.id}'"
    if step.tool == tool:
        moves = _next_text(step, _previous(run))
        if not moves:
            return None
        # "If one applies": a tool that failed may leave no move to make.
        return (
            f"{at}, whose tool this was. If one of its moves now applies, "
            f"report it with {MOVE_TO_STEP} before you reply: {moves}. If none "
            f"does, stay on '{step.id}'."
        )
    # ponytail: one move ahead only; a tool further on is a skipped step,
    # which the move's refusal names.
    # Never an escalate step: it runs its action on the move, so reported
    # after its tool already ran, the transfer or handoff would run twice.
    ahead = next(
        (
            s
            for s, _ in steps_ahead(procedure, step, 1)
            if s.tool == tool and s.kind != StepKind.ESCALATE
        ),
        None,
    )
    if ahead is None:
        return None
    # "If you called it for that step": the same tool may answer a side
    # question (a menu search) with the step still ahead.
    message = (
        f"{at}, and this is the tool of step '{ahead.id}', one move ahead. If "
        f"you called it for that step, report the move to {ahead.id} with "
        f"{MOVE_TO_STEP}"
    )
    if moves_after := _next_text(ahead, None):
        return f"{message}, then the move after it, before you reply: {moves_after}."
    return f"{message} before you reply."


def _previous(run: ProcedureRun) -> str | None:
    return run.path[-2] if len(run.path) > 1 else None


def _stayed(
    state: ProcedureState, procedure: str, current: ProcedureStep, back: str | None
) -> MoveOutcome:
    """A move to where the procedure already is: the model staying on the
    step (a caller who asked to hear it again). Nothing moves; the result
    says when to move on, so the model need not try again."""
    message = (
        f"Already at step '{current.id}': stay on it, and move only when one "
        f"of its moves applies. {_allowed_text(current, back)}"
    )
    details = {"procedure": procedure, "step": current.id, "stayed": True}
    return MoveOutcome(
        accepted=False,
        state=state,
        procedure=procedure,
        from_step=current.id,
        to_step=current.id,
        kind=None,
        result=_result(True, message, details),
        stayed=True,
    )


def _start(state: ProcedureState, graph: Procedure, to: str) -> MoveOutcome:
    """A move in a procedure not running: the model starting an `on_request`
    one (#446), at its start step and from nowhere. Any other move in it is
    refused, naming the start."""
    start = graph.start_step if graph.start == ProcedureStart.ON_REQUEST else None
    if start is None or to != start.id:
        names = [r.name for r in state.runs]
        how = f" Start it at its start step '{start.id}'." if start else ""
        message = f"'{graph.name}' is not running.{how} Running: {names}."
        return _refused(state, graph.name, None, to, message, start_at=start)
    return MoveOutcome(
        accepted=True,
        state=state._placed(ProcedureRun(graph.name, (start.id,))),
        procedure=graph.name,
        from_step=None,
        to_step=start.id,
        kind=start.kind,
        result=_result(
            True,
            f"Started '{graph.name}'. {_arrived(start)}",
            {"procedure": graph.name, "step": start.id},
        ),
    )


def _arrived(step: ProcedureStep, back: str | None = None) -> str:
    """What the model reads on reaching `step`: what to do there and, since
    a step tool's result comes with no window, where to move once done."""
    message = f"Now at step '{step.id}' ({step.kind}). Do now: {step.text}"
    if step.tool:
        message += f" Call {step.tool} at this step."
    if step.kind in ENDING_STEP_KINDS:
        return message + " The procedure is finished here."
    if nexts := _next_text(step, back):
        message += f" Then move on: {nexts}."
    return message


def _next_text(step: ProcedureStep, back: str | None) -> str:
    parts = [f"{m.to} (when {m.when})" for m in step.moves]
    if back is not None and back not in {m.to for m in step.moves}:
        parts.append(f"back to {back}")
    return "; ".join(parts)


def _allowed(current: ProcedureStep | None, back: str | None) -> list[str]:
    moves = [m.to for m in current.moves] if current is not None else []
    return [*moves, back] if back is not None and back not in moves else moves


def _allowed_text(current: ProcedureStep | None, back: str | None) -> str:
    if current is None:
        return "No move is allowed from here."
    if not (parts := _next_text(current, back)):
        return f"No move is allowed from '{current.id}'."
    return f"Allowed from '{current.id}': {parts}."


def _refused(
    state: ProcedureState,
    procedure: str,
    at: str | None,
    to: str,
    message: str,
    current: ProcedureStep | None = None,
    back: str | None = None,
    *,
    start_at: ProcedureStep | None = None,
) -> MoveOutcome:
    allowed = [start_at.id] if start_at is not None else _allowed(current, back)
    details = {"procedure": procedure, "at": at, "allowed": allowed}
    return MoveOutcome(
        accepted=False,
        state=state,
        procedure=procedure,
        from_step=at,
        to_step=to,
        kind=None,
        result=_result(False, message, details),
    )


def _result(success: bool, message: str, details: dict[str, Any]) -> str:
    return json.dumps({"success": success, "message": message, "details": details})
