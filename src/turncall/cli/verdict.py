"""What the CLI exits with, and why (#77).

The reason this is its own module: the exit code is the whole point of the
command, and it should be checkable without a server, a database or a worker.

Three outcomes, not two. A failed run means the agent did something wrong; an
errored run means nobody could tell — a judge that was unreachable, a pipeline
that died. Both exit non-zero, because neither is a green light, but a CI log
that calls an outage a regression sends someone hunting a bug that does not
exist.

Four, really (#112). A run still going when the command stopped waiting is
neither of those: it is not terminal, the worker is still running it, and it
will reach a real verdict minutes later that nobody will read because the
pipeline has already gone red. The remedy differs too — an outage means check
the judge, a timeout means raise `--timeout` or give the worker more slots — so
folding it into `errored` sends someone hunting an outage that never happened.
"""

from __future__ import annotations

from typing import Any

# 0 is the only success. Everything else is distinguishable so a pipeline can
# branch on it: "the agent regressed" and "we could not check" want different
# alerts, and a run cancelled out from under the command is neither.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERRORED = 2
EXIT_CANCELLED = 3
# Non-terminal at the deadline: the runs are still going, so there is no
# verdict yet rather than no verdict possible.
EXIT_TIMEOUT = 4
EXIT_USAGE = 64  # sysexits.h EX_USAGE, for a command that was never runnable

_TERMINAL = ("passed", "failed", "errored", "cancelled")


def is_terminal(status: str) -> bool:
    return status in _TERMINAL


def exit_code(runs: list[dict[str, Any]]) -> int:
    """The code for a finished batch.

    Zero if and only if every run passed — the promise the whole command is
    built on. Precedence below zero is by how loudly it should be read: a
    failure is a claim about the agent and outranks an error, which is a claim
    about the harness, which outranks a run that simply has not finished. A
    suite where one scenario regressed and another was slow has regressed.
    """
    statuses = {run.get("status") for run in runs}
    if not runs:
        # Nothing ran. Reporting success for an empty batch is how a green
        # pipeline stops meaning anything.
        return EXIT_ERRORED
    if statuses == {"passed"}:
        return EXIT_OK
    if "failed" in statuses:
        return EXIT_FAILED
    if "errored" in statuses:
        return EXIT_ERRORED
    if "cancelled" in statuses:
        return EXIT_CANCELLED
    # Still running: the caller asked for a verdict before there was one (#112).
    # Distinct from `errored`, which means one can never come.
    return EXIT_TIMEOUT


def summarise(runs: list[dict[str, Any]]) -> dict[str, int]:
    """Counts by status, every terminal status present even at zero.

    A summary that omits `errored` when it is zero trains people not to look
    for it when it is not.
    """
    counts = {status: 0 for status in _TERMINAL}
    for run in runs:
        status = str(run.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def summary_line(runs: list[dict[str, Any]]) -> str:
    """One line a CI log can be grepped for."""
    counts = summarise(runs)
    total = len(runs)
    parts = [f"{counts['passed']}/{total} passed"]
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    # Always named when present, never folded into "failed": an outage is not
    # a regression.
    if counts["errored"]:
        parts.append(f"{counts['errored']} errored (could not check)")
    if counts["cancelled"]:
        parts.append(f"{counts['cancelled']} cancelled")
    # Named separately for the same reason `errored` is: a line that folds
    # "we stopped waiting" into either verdict teaches people to read it wrong.
    unfinished = total - sum(counts[status] for status in _TERMINAL)
    if unfinished:
        parts.append(f"{unfinished} still running (timed out waiting)")
    return ", ".join(parts)
