"""Every voice edge opens and runs its calls through the call module (#487).

Opening a call (`services/call_opening`) owns agent acceptance, the live-call
slot, the call row and its first events; running one
(`orchestrator/call_runner.run_call`) owns the drain count, the slot's hold,
MCP, the ambience mixer, the call context, the build and the start. What each
of those does is tested through the modules themselves
(`tests/integration/test_call_opening.py`, `test_run_call.py`,
`test_outbound_opening.py`, `test_whatsapp_voice_opening.py`).

What those tests cannot see is an edge that skips the module and does the
steps itself — the copies this replaced, which drifted (#488, #489). So this
is the one structural check left: a module that builds a voice transport hands
it to `run_call`, only the running module builds a call's pipeline, and only
the opening module writes a call row. It fails naming the module.
"""

import ast
from pathlib import Path

import pytest

import turncall
from turncall.orchestrator import transport_factory as tf

pytestmark = pytest.mark.unit

SRC = Path(turncall.__file__).parent
VOICE_FACTORIES = {
    name
    for name, f in vars(tf).items()
    if name.startswith("create_")
    and name.endswith("_transport")
    and callable(f)
    # An eval is not a call: no row, no slot (ADR-0018).
    and name != "create_eval_transport"
}


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text())
        for path in sorted(SRC.rglob("*.py"))
    }


def _called(tree: ast.Module) -> set[str]:
    return {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }


def _named(tree: ast.Module) -> set[str]:
    """Called or handed over: an edge passes its factory to `run_call`."""
    return {
        getattr(node, "id", None) or getattr(node, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }


def _calling(name: str) -> set[str]:
    return {rel for rel, tree in _modules().items() if name in _called(tree)}


def test_every_voice_edge_runs_its_calls_through_run_call() -> None:
    edges = {
        rel
        for rel, tree in _modules().items()
        if _named(tree) & VOICE_FACTORIES and rel != "orchestrator/transport_factory.py"
    }
    # Every carrier's media stream, WebRTC, WhatsApp voice and the demo.
    assert {
        "webhooks/media_stream.py",
        "api/v1/webrtc.py",
        "webhooks/whatsapp_handlers.py",
        "demo/app.py",
    } <= edges, edges
    skipping = sorted(
        rel for rel in edges if "run_call" not in _called(_modules()[rel])
    )
    assert skipping == [], (
        f"{skipping} build a voice transport without handing the call to "
        "call_runner.run_call: its calls would miss the steps running owns "
        "(drain, live-call slot, MCP close, mixer, call context)"
    )


def test_only_the_running_module_builds_a_calls_pipeline() -> None:
    builders = _calling("build_call_pipeline") - {"orchestrator/pipeline_builder.py"}
    # The eval harness builds one for an eval, which is not a call (ADR-0018).
    assert builders == {"orchestrator/call_runner.py", "evals/harness.py"}, builders


def test_only_the_opening_module_writes_a_call_row() -> None:
    """An outbound dial writes its row through it too (#495)."""
    writers = _calling("create_call") - {"storage/repositories/call_repo.py"}
    assert writers == {"services/call_opening.py"}, writers
