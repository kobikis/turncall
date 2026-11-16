"""Regression guard for review finding #1: KB retrieval must stay wired into
the voice pipeline.

Every voice call is built by `pipeline_builder.build_call_pipeline`, through
the running module (`test_voice_edges_use_the_call_module.py` holds every
edge to it). So one check is left: the shared builder's `create_pipeline(...)`
call must pass `knowledge_base_attachments`, or KB auto/tool retrieval goes
dead on every transport at once.

Structural (AST) so it survives renames and needs no transport/DB.
"""

import ast
from pathlib import Path

import pytest

import turncall

_BUILDER = Path(turncall.__file__).parent / "orchestrator" / "pipeline_builder.py"


def _calls_named(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
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
