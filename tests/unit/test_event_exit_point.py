"""Every call event leaves through one exit point (#312, ADR-0024).

What this guards: a row on a call's event log (`call_events`) and a webhook
to a subscriber (`dispatcher.dispatch_event`) are written only through
`events/emit.py`, so redaction (#313) covers them by standing there. It reads
the engine's source and fails on any other module that stores or dispatches
an event: by naming one of the ways out, constructing a `CallEventRow`,
reaching a way out through `getattr` with its name as a string, or writing
`call_events` in raw SQL.

What it does not guard, because the exit point does not cover it: call-init
and the other server events (`events/server_events.py`) and tool webhooks,
which are part of the live call and carry raw values to the operator's own
server; and tool-invocation rows (`tool_invocation_repo.create_invocation`),
stored data whose redaction is #314's. A static scan is not a proof either:
a way out reached through computed names would get past it.

The end-to-end half is `tests/integration/test_harness_event_exit_point.py`.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "turncall"
EXIT_POINT = "events/emit.py"
CALL_REPO = "storage/repositories/call_repo.py"

# Each way out, and the only modules that may name it: where it is defined,
# and the exit point that calls it.
WAYS_OUT = {
    "create_call_event": {CALL_REPO, EXIT_POINT},
    "get_next_sequence_number": {CALL_REPO, EXIT_POINT},
    "CallEventRow": {"storage/models.py", CALL_REPO},
    "dispatch_event": {"events/dispatcher.py", EXIT_POINT},
    "deliver_to_subscribers": {"events/webhook_delivery.py", "events/dispatcher.py"},
    "deliver_webhook": {"events/webhook_delivery.py"},
    "WebhookEvent": {"events/webhook_delivery.py", "events/dispatcher.py"},
}
# Where a `CallEventRow(...)` may be built: the one function that inserts it.
BUILDS_A_ROW = {CALL_REPO}
# Raw SQL that adds to or rewrites the log. A DELETE takes nothing out of the
# call, so it is not a way out.
WRITES_THE_LOG = re.compile(
    r"\b(insert\s+into|update|copy)\s+(\w+\.)?call_events\b", re.IGNORECASE
)


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ways_out(tree: ast.AST) -> set[str]:
    """Every way out a module reaches, by any of the routes the guard knows."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Call):
            called = _called(node)
            if called == "CallEventRow":
                found.add("CallEventRow(")
            elif (
                called in {"getattr", "setattr", "hasattr"}
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                found.add(node.args[1].value)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and WRITES_THE_LOG.search(node.value)
        ):
            found.add("SQL writing call_events")
    return found


ALLOWED = {**WAYS_OUT, "CallEventRow(": BUILDS_A_ROW, "SQL writing call_events": set()}


def _mentions(source: dict[str, str] | None = None) -> dict[str, set[str]]:
    """Module (relative to the package) -> the ways out it reaches."""
    if source is None:
        source = {
            path.relative_to(SRC).as_posix(): path.read_text()
            for path in SRC.rglob("*.py")
        }
    out: dict[str, set[str]] = {}
    for module, text in source.items():
        found = _ways_out(ast.parse(text, filename=module)) & set(ALLOWED)
        if found:
            out[module] = found
    return out


def _strays(source: dict[str, str] | None = None) -> list[str]:
    return sorted(
        f"{module} uses {way}"
        for module, ways in _mentions(source).items()
        for way in ways
        if module not in ALLOWED[way]
    )


def test_nothing_stores_or_sends_an_event_but_the_exit_point() -> None:
    strays = _strays()
    assert not strays, (
        "store or send events through turncall.events.emit "
        "(emit_call_event, record_call_event, dispatch_call_event, "
        f"dispatch_project_event): {strays}"
    )


def test_the_guard_sees_the_exit_point() -> None:
    """A rename must not leave the guard above checking for nothing."""
    assert {"create_call_event", "dispatch_event"} <= _mentions()[EXIT_POINT]


@pytest.mark.parametrize(
    "code",
    [
        "from turncall.storage.repositories.call_repo import create_call_event",
        "async def f(repo):\n    await repo.create_call_event(None)",
        "from turncall.storage import models\nrow = models.CallEventRow(call_id=1)",
        "async def f(d):\n    await getattr(d, 'dispatch_event')(None)",
        "from sqlalchemy import text\nq = text('INSERT INTO call_events (id) VALUES (1)')",
        "q = 'update public.call_events set payload = :p'",
    ],
)
def test_the_guard_catches_each_route(code: str) -> None:
    assert _strays({"services/stray.py": code})


def test_the_guard_lets_a_read_and_a_delete_through() -> None:
    assert not _strays(
        {
            "services/reader.py": (
                "from turncall.storage.repositories import call_repo\n"
                "async def f(s, c):\n"
                "    await call_repo.list_call_events(s, c)\n"
                "q = 'DELETE FROM call_events WHERE call_id = :c'\n"
            )
        }
    )
