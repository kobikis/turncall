"""Regression guard for review finding #1: KB retrieval must stay wired into
every voice path.

The pipeline assembly is now centralized in orchestrator/pipeline_builder.py
(build_call_pipeline), so the guard is two-part:
  1. the shared builder's create_pipeline(...) call MUST pass
     knowledge_base_attachments — else KB auto/tool retrieval goes dead
     everywhere at once;
  2. every voice transport MUST route through the shared assembly — else a
     transport silently stops using the centralized (KB-wired) one. Twilio
     calls build_call_pipeline directly (its WS handler awaits the run);
     WebRTC and WhatsApp go via start_call_pipeline, which wraps it in the
     task that also owns their MCP sessions.

Structural (AST) so it survives renames and needs no transport/DB.
"""

import ast
from pathlib import Path

import pytest

import turncall

_SRC = Path(turncall.__file__).parent
_BUILDER = _SRC / "orchestrator" / "pipeline_builder.py"

# (label, file) for every entry point that starts a voice pipeline.
VOICE_PATHS = [
    ("twilio", _SRC / "webhooks" / "media_stream.py"),
    ("whatsapp", _SRC / "webhooks" / "whatsapp_handlers.py"),
    ("webrtc", _SRC / "api" / "v1" / "webrtc.py"),
]


def _calls_named(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if fname == name:
                out.append(node)
    return out


@pytest.mark.unit
def test_shared_builder_passes_kb_attachments() -> None:
    calls = _calls_named(_BUILDER, "create_pipeline")
    assert calls, "pipeline_builder: no create_pipeline() call found — assembly moved?"
    for call in calls:
        kwargs = {k.arg for k in call.keywords}
        assert "knowledge_base_attachments" in kwargs, (
            "pipeline_builder: create_pipeline() is missing "
            "knowledge_base_attachments — KB retrieval is dead on every transport"
        )


@pytest.mark.unit
@pytest.mark.parametrize("label,path", VOICE_PATHS, ids=[p[0] for p in VOICE_PATHS])
def test_voice_path_uses_shared_builder(label: str, path: Path) -> None:
    entry_points = ("build_call_pipeline", "start_call_pipeline")
    assert any(_calls_named(path, name) for name in entry_points), (
        f"{label} ({path.name}): calls none of {entry_points} — this transport "
        "no longer routes through the centralized (KB-wired) pipeline assembly"
    )


@pytest.mark.unit
def test_start_call_pipeline_delegates_to_the_builder() -> None:
    """start_call_pipeline is the second entry point the guard above accepts,
    so it has to actually reach the KB-wired assembly."""
    assert _calls_named(_BUILDER, "start_call_pipeline") == [], (
        "start_call_pipeline should not call itself"
    )
    tree = ast.parse(_BUILDER.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_call_pipeline"
    )
    called = {
        getattr(c.func, "id", getattr(c.func, "attr", None))
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    }
    assert "build_call_pipeline" in called, (
        "start_call_pipeline no longer calls build_call_pipeline — WebRTC and "
        "WhatsApp would lose the centralized (KB-wired) assembly"
    )
