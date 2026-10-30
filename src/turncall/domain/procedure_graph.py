"""The rule a procedure graph is held to (#444): one function, applied twice.

The agent API runs it at save and answers 422 (`AgentConfigSchema`); the
pipeline build runs it for a call-init inline agent, which skipped that
schema, and fails the call as `pipeline_error` naming the same problem
(`pipeline_builder._assemble`) — the `xai_config` refusal pattern. So a
stored agent never reaches a call with a broken graph, and an inline one
never runs half of one.

A refusal names the procedure by index and name and, where there is one, the
step: `procedures[0] 'no_internet': step 'after_restart' moves to
'technican', which does not exist`.

Each check is one function of one procedure in `_CHECKS`, the first problem
wins. A new rule is one more function there; one that needs more than the
procedure (the agent's tools, say) takes a keyword on `procedures_refusal`
and joins the tuple as a closure.

No pipecat here: the API schema imports this module.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from uuid import UUID

from turncall.domain.enums import (
    ENDING_STEP_KINDS,
    ProcedureStart,
    StepKind,
    TransferMode,
)
from turncall.domain.procedures import (
    HANDOFF_TO_AGENT,
    TRANSFER_CALL,
    EscalationAction,
    Procedure,
)

# How many steps one procedure may have, and how far ahead its window may
# reach. The window is in the context on every turn, so both bound what each
# turn costs; a graph past either belongs in more than one procedure.
MAX_STEPS = 50
MAX_WINDOW_DEPTH = 5

# An escalate step's transfer target: the API's `E164_PATTERN`, for an inline
# agent that skipped it.
_E164 = re.compile(r"\+[1-9]\d{1,14}")

Check = Callable[[Procedure], str | None]


def procedures_refusal(
    procedures: Sequence[Procedure], *, callable_tools: Iterable[str] | None = None
) -> str | None:
    """The first thing wrong with an agent's procedures, or None.

    `callable_tools`, when the caller knows them, is every name the agent can
    call, and each step's `tool` must be one (#447). The API passes them for
    an agent with no MCP server; the build passes none.
    """
    checks = _CHECKS
    if callable_tools is not None:
        checks = (*checks, _step_tools_in(frozenset(callable_tools)))
    seen: set[str] = set()
    for index, procedure in enumerate(procedures):
        where = f"procedures[{index}]" + (
            f" '{procedure.name}'" if procedure.name else ""
        )
        if procedure.name in seen:
            return f"{where}: another procedure is already named '{procedure.name}'"
        seen.add(procedure.name)
        for check in checks:
            if (problem := check(procedure)) is not None:
                return f"{where}: {problem}"
    return None


def _readable(procedure: Procedure) -> str | None:
    if procedure.unreadable is None:
        return None
    return f"cannot be read: {procedure.unreadable}"


def _known_words(procedure: Procedure) -> str | None:
    # The API schema's Literals refuse these first; an inline agent's config
    # parses any string here.
    if procedure.start not in ProcedureStart:
        return f"start '{procedure.start}' must be one of {_words(ProcedureStart)}"
    for step in procedure.steps:
        if step.kind not in StepKind:
            return (
                f"step '{step.id}' has kind '{step.kind}'; it must be one of "
                f"{_words(StepKind)}"
            )
    return None


def _words(words: Iterable[str]) -> str:
    return ", ".join(str(word) for word in words)


def _within_the_caps(procedure: Procedure) -> str | None:
    if len(procedure.steps) > MAX_STEPS:
        return (
            f"{len(procedure.steps)} steps, more than the {MAX_STEPS} a "
            "procedure may have"
        )
    if procedure.window_depth < 0:
        return f"window_depth {procedure.window_depth} is less than 0"
    if procedure.window_depth > MAX_WINDOW_DEPTH:
        return f"window_depth {procedure.window_depth} is more than {MAX_WINDOW_DEPTH}"
    return None


def _unique_ids(procedure: Procedure) -> str | None:
    counts = Counter(step.id for step in procedure.steps)
    repeated = next((sid for sid, n in counts.items() if n > 1), None)
    if repeated is None:
        return None
    return f"step id '{repeated}' is used by more than one step"


def _one_start(procedure: Procedure) -> str | None:
    starts = [step.id for step in procedure.steps if step.start]
    if not starts:
        return "no step is the start step; mark exactly one with start: true"
    if len(starts) > 1:
        return (
            f"steps '{starts[0]}' and '{starts[1]}' are both marked start; mark "
            "exactly one"
        )
    return None


def _moves_land(procedure: Procedure) -> str | None:
    ids = {step.id for step in procedure.steps}
    for step in procedure.steps:
        for target in (m.to for m in step.moves):
            if target not in ids:
                return f"step '{step.id}' moves to '{target}', which does not exist"
    return None


def _has_an_ending(procedure: Procedure) -> str | None:
    if any(step.kind in ENDING_STEP_KINDS for step in procedure.steps):
        return None
    return (
        "no step ends it; give it at least one step of kind "
        f"{' or '.join(repr(str(k)) for k in ENDING_STEP_KINDS)}"
    )


def _all_reachable(procedure: Procedure) -> str | None:
    """Every step reachable from the start by its moves. Moving back only
    revisits a step already reached, so it reaches nothing new."""
    start = procedure.start_step
    if start is None:  # _one_start refuses it first
        return None
    reached = {start.id}
    frontier = [start.id]
    while frontier:
        step = procedure.step(frontier.pop())
        for target in (m.to for m in step.moves) if step is not None else ():
            if target not in reached:
                reached.add(target)
                frontier.append(target)
    unreached = next((s.id for s in procedure.steps if s.id not in reached), None)
    if unreached is None:
        return None
    return f"step '{unreached}' cannot be reached from the start step '{start.id}'"


def _escalations(procedure: Procedure) -> str | None:
    """Each `escalate` step names one action, and only those do (#448).
    Reaching the step runs it, so an inline agent's action is checked as the
    API schema checks a stored one's: a mode the transfer knows, an E.164
    target, an agent id that is one."""
    for step in procedure.steps:
        actions = step.escalate.actions() if step.escalate is not None else []
        if step.kind != StepKind.ESCALATE:
            if actions:
                return (
                    f"step '{step.id}' is of kind '{step.kind}'; only an "
                    "escalate step takes an escalate action"
                )
            continue
        if not actions:
            return (
                f"step '{step.id}' escalates with no action; give it "
                f"escalate.{TRANSFER_CALL} or escalate.{HANDOFF_TO_AGENT}"
            )
        if len(actions) > 1:
            return (
                f"step '{step.id}' names both a transfer and a handoff; an "
                "escalate step does one"
            )
        if (problem := _action_problem(actions[0])) is not None:
            return f"step '{step.id}' {problem}"
    return None


def _action_problem(action: EscalationAction) -> str | None:
    tool, args = action
    if tool == HANDOFF_TO_AGENT:
        try:
            UUID(args["agent_id"])
        except ValueError:
            return f"hands off to '{args['agent_id']}', which is not an agent id"
        return None
    mode = args.get("transfer_mode", TransferMode.COLD)
    if mode not in TransferMode:
        return f"transfers with mode '{mode}'; it must be one of {_words(TransferMode)}"
    if not _E164.fullmatch(args["target_number"]):
        return f"transfers to '{args['target_number']}', which is not an E.164 number"
    return None


def _step_tools_in(callable_tools: frozenset[str]) -> Check:
    """A step's `tool` is one the agent can call (#447): its own tools and
    the built-ins. A step names one of them; it is never a tool of its own."""

    def check(procedure: Procedure) -> str | None:
        for step in procedure.steps:
            if step.tool is not None and step.tool not in callable_tools:
                return (
                    f"step '{step.id}' names tool '{step.tool}', which is not "
                    "one of the agent's tools or a built-in"
                )
        return None

    return check


# In order: each check may rely on the ones before it having passed.
_CHECKS: tuple[Check, ...] = (
    _readable,
    _known_words,
    _within_the_caps,
    _unique_ids,
    _one_start,
    _moves_land,
    _has_an_ending,
    _all_reachable,
    _escalations,
)
