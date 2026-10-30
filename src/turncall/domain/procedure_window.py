"""The procedure window (#442, ADR-0032): what the model is shown of a
procedure on one turn.

The current step and what to do there, the moves allowed from it with their
conditions, the move back to the previous step, and a preview of the steps up
to `depth` ahead — never the rest of the graph. A pure function of the graph,
the visited path and the depth, so every carrier and WebRTC get the same text.
"""

from collections.abc import Sequence

from turncall.domain.enums import ProcedureStart, StepKind
from turncall.domain.procedures import MOVE_TO_STEP, Procedure, ProcedureStep

# What the window's context message starts with, so the next turn finds it
# to replace (`orchestrator/turn_message.py`).
WINDOW_MARKER = "[Procedure]"

_RULES = (
    f"Report every move with {MOVE_TO_STEP}. Do not skip a step. If the "
    "caller's answer does not fit any move, ask again."
)

STARTABLE_HEADING = "## Procedures you can start"
STARTABLE_HINT = (
    "When one applies, start it with "
    f"{MOVE_TO_STEP}(procedure=<its name>, to=<its start step>); its steps "
    "are then shown to you one at a time."
)


def build_window(procedure: Procedure, path: Sequence[str], *, depth: int) -> str:
    """The window at `path[-1]`, having come by `path`."""
    current = procedure.step(path[-1])
    if current is None:
        return ""
    lines = [
        f'Procedure "{procedure.name}" — current step: {current.id} ({_kind(current)})',
        f"  Do now: {current.text}",
        *_moves(current, path),
        *_preview(procedure, current, depth),
        f"  {_RULES}",
        *_escalation(procedure, current),
    ]
    return "\n".join(lines)


def _escalation(procedure: Procedure, current: ProcedureStep) -> list[str]:
    """When a move from here escalates, the line saying when to take it: a
    model told only the condition tends to keep trying instead."""
    targets = [
        m.to
        for m in current.moves
        if (step := procedure.step(m.to)) is not None and step.kind == StepKind.ESCALATE
    ]
    if not targets:
        return []
    return [
        "  If the procedure cannot resolve the caller's problem, escalate: "
        f"move to {' or '.join(targets)}."
    ]


def window_message(windows: Sequence[str]) -> str:
    """One context message for every procedure running: the marker, then
    each window."""
    return "\n\n".join([WINDOW_MARKER, *(w for w in windows if w)])


def startable_section(procedures: Sequence[Procedure]) -> str | None:
    """The prompt section naming the `on_request` procedures the model can
    start (#446): each one's name, description and start step id — never a
    step's text, which only its window shows once it runs. None with none."""
    lines = [
        f"- {p.name}: {p.description} (start step: {start.id})"
        for p in procedures
        if p.start == ProcedureStart.ON_REQUEST and (start := p.start_step) is not None
    ]
    if not lines:
        return None
    return "\n".join([STARTABLE_HEADING, STARTABLE_HINT, *lines])


def _kind(step: ProcedureStep) -> str:
    return f"{step.kind}, tool: {step.tool}" if step.tool else step.kind


def _moves(current: ProcedureStep, path: Sequence[str]) -> list[str]:
    lines = [f"    → {m.to}  when: {m.when}" for m in current.moves]
    if len(path) > 1:
        lines.append(f"    ← back to {path[-2]}  (always allowed)")
    if not lines:
        return ["  Allowed moves: none — the procedure ends here."]
    return ["  Allowed moves:", *lines]


def _preview(procedure: Procedure, current: ProcedureStep, depth: int) -> list[str]:
    ahead = _ahead(procedure, current, depth)
    if not ahead:
        return []
    lines = []
    for step, came_from in ahead:
        where = "" if came_from == current.id else f" — after {came_from}"
        lines.append(f"    {step.id} ({_kind(step)}): {step.text}{where}")
    return ["  Next steps (preview):", *lines]


def _ahead(
    procedure: Procedure, current: ProcedureStep, depth: int
) -> list[tuple[ProcedureStep, str]]:
    """The steps up to `depth` moves ahead, nearest first, each with the step
    it is reached from; each step once, and never the current one."""
    seen = {current.id}
    frontier = [current]
    found: list[tuple[ProcedureStep, str]] = []
    for _ in range(max(depth, 0)):
        reached = []
        for step in frontier:
            for move in step.moves:
                target = procedure.step(move.to)
                if target is None or target.id in seen:
                    continue
                seen.add(target.id)
                reached.append(target)
                found.append((target, step.id))
        frontier = reached
    return found
