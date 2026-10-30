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

from turncall.domain.procedure_window import build_window, window_message
from turncall.domain.procedures import Procedure, ProcedureStep


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
    """The agent's procedures and those of them running, in config order."""

    procedures: tuple[Procedure, ...] = ()
    runs: tuple[ProcedureRun, ...] = ()

    @classmethod
    def at_call_start(cls, procedures: Sequence[Procedure]) -> "ProcedureState":
        """Every `start: "call"` procedure running, at its start step."""
        runs = tuple(
            ProcedureRun(p.name, (p.start_step.id,))
            for p in procedures
            if p.start == "call" and p.start_step is not None
        )
        return cls(procedures=tuple(procedures), runs=runs)

    def procedure(self, name: str) -> Procedure | None:
        return next((p for p in self.procedures if p.name == name), None)

    def run(self, name: str) -> ProcedureRun | None:
        return next((r for r in self.runs if r.name == name), None)

    def window_text(self) -> str | None:
        """This turn's window message, or None with nothing running."""
        windows = [
            build_window(procedure, run.path, depth=procedure.window_depth)
            for run in self.runs
            if (procedure := self.procedure(run.name)) is not None
        ]
        return window_message(windows) if windows else None

    def _with_run(self, run: ProcedureRun) -> "ProcedureState":
        runs = tuple(run if r.name == run.name else r for r in self.runs)
        return replace(self, runs=runs)


@dataclass(frozen=True)
class MoveOutcome:
    """What a reported move did: the state after it (the same one when
    refused) and the tool result the model reads."""

    accepted: bool
    state: ProcedureState
    procedure: str
    from_step: str | None
    to_step: str
    # The target step's kind, for an accepted move.
    kind: str | None
    result: str


def move(state: ProcedureState, procedure: str, to: str) -> MoveOutcome:
    """Move `procedure` to step `to`: one of the current step's moves, or
    back to the previous step. Anything else is refused, naming what is
    allowed."""
    run = state.run(procedure)
    graph = state.procedure(procedure)
    if graph is None or run is None:
        names = [r.name for r in state.runs]
        reason = (
            f"'{procedure}' is not running"
            if graph is not None
            else f"There is no procedure named '{procedure}'"
        )
        return _refused(state, procedure, None, to, f"{reason}. Running: {names}.")
    current = graph.step(run.current)
    target = graph.step(to)
    back = run.path[-2] if len(run.path) > 1 else None
    if current is None or target is None:
        message = f"'{procedure}' has no step '{to}'. {_allowed_text(current, back)}"
        return _refused(state, procedure, run.current, to, message, current, back)
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
        state=state._with_run(moved),
        procedure=procedure,
        from_step=run.current,
        to_step=to,
        kind=target.kind,
        result=_result(True, _arrived(target), {"procedure": procedure, "step": to}),
    )


def _arrived(step: ProcedureStep) -> str:
    message = f"Now at step '{step.id}' ({step.kind}). Do now: {step.text}"
    if step.tool:
        message += f" Call {step.tool} at this step."
    return message


def _allowed(current: ProcedureStep | None, back: str | None) -> list[str]:
    moves = [m.to for m in current.moves] if current is not None else []
    return [*moves, back] if back is not None else moves


def _allowed_text(current: ProcedureStep | None, back: str | None) -> str:
    if current is None:
        return "No move is allowed from here."
    parts = [f"{m.to} (when {m.when})" for m in current.moves]
    if back is not None:
        parts.append(f"back to {back}")
    if not parts:
        return f"No move is allowed from '{current.id}'."
    return f"Allowed from '{current.id}': {'; '.join(parts)}."


def _refused(
    state: ProcedureState,
    procedure: str,
    at: str | None,
    to: str,
    message: str,
    current: ProcedureStep | None = None,
    back: str | None = None,
) -> MoveOutcome:
    details = {"procedure": procedure, "at": at, "allowed": _allowed(current, back)}
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
