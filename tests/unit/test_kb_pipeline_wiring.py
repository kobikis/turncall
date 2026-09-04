"""Regression guard for review finding #1: KB retrieval must stay wired into
every voice path.

The pipeline assembly is now centralized in orchestrator/pipeline_builder.py
(build_call_pipeline), so the guard is two-part:
  1. the shared builder's create_pipeline(...) call MUST pass
     knowledge_base_attachments — else KB auto/tool retrieval goes dead
     everywhere at once;
  2. every voice transport MUST route through build_call_pipeline — else a
     transport silently stops using the centralized (KB-wired) assembly.

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
    assert _calls_named(path, "build_call_pipeline"), (
        f"{label} ({path.name}): no build_call_pipeline() call — this transport "
        "no longer routes through the centralized (KB-wired) pipeline assembly"
    )
