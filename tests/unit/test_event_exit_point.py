"""Every event leaves through one exit point (#312, ADR-0024).

A row on a call's event log and a webhook to a subscriber are what leave a
call, and redaction (#313) can only cover them if they all pass one place:
`events/emit.py`. This reads the engine's source and fails on any module that
writes a call event or dispatches one some other way. The end-to-end half is
`tests/integration/test_harness_event_exit_point.py`.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "turncall"
EXIT_POINT = "events/emit.py"

# Each way out, and the only modules that may name it: where it is defined,
# and the exit point that calls it.
WAYS_OUT = {
    "create_call_event": {"storage/repositories/call_repo.py", EXIT_POINT},
    "get_next_sequence_number": {"storage/repositories/call_repo.py", EXIT_POINT},
    "CallEventRow": {"storage/models.py", "storage/repositories/call_repo.py"},
    "dispatch_event": {"events/dispatcher.py", EXIT_POINT},
    "deliver_to_subscribers": {"events/webhook_delivery.py", "events/dispatcher.py"},
    "deliver_webhook": {"events/webhook_delivery.py"},
    "WebhookEvent": {"events/webhook_delivery.py", "events/dispatcher.py"},
}


def _names(tree: ast.AST) -> set[str]:
    """Every name a module mentions: bare, as an attribute, or imported."""
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
    return found


def _mentions() -> dict[str, set[str]]:
    """Module (relative to the package) -> the ways out it names."""
    out: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        module = path.relative_to(SRC).as_posix()
        named = _names(ast.parse(path.read_text(), filename=str(path))) & set(WAYS_OUT)
        if named:
            out[module] = named
    return out


def test_nothing_stores_or_sends_an_event_but_the_exit_point() -> None:
    strays = sorted(
        f"{module} uses {name}"
        for module, names in _mentions().items()
        for name in names
        if module not in WAYS_OUT[name]
    )
    assert not strays, (
        "store or send events through turncall.events.emit "
        "(emit_call_event, record_call_event, dispatch_call_event, "
        f"dispatch_project_event): {strays}"
    )


def test_the_guard_sees_the_exit_point() -> None:
    """A rename must not leave the guard above checking for nothing."""
    assert {"create_call_event", "dispatch_event"} <= _mentions()[EXIT_POINT]
