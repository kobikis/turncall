"""Regression guard: a call running an inline agent must still build a pipeline.

Managed call-init can answer with an inline `agent` config instead of an
`agent_id` (ADR-0008, and the integrations work that finally exercises it). Such
a call has no agent row, so `CallContext.agent_id` has nothing real to carry and
every transport substitutes `DYNAMIC_AGENT_ID`.

Twilio did not. `media_stream.py` built its context with `UUID(agent_id_str)`,
where `agent_id_str` was the literal string "dynamic" on exactly that path, so
every inline-agent call over Twilio died with `ValueError: badly formed
hexadecimal UUID string` before the pipeline started. WebRTC and WhatsApp voice
had each invented their own `UUID(int=0)` in the same place and worked.

Structural (AST), like test_kb_pipeline_wiring, so it survives renames and needs
no transport or DB.
"""

import ast
from pathlib import Path
from uuid import UUID

import pytest

import turncall
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID

_SRC = Path(turncall.__file__).parent

# (label, file) for every entry point that builds a CallContext for a voice call.
VOICE_PATHS = [
    ("twilio", _SRC / "webhooks" / "media_stream.py"),
    ("whatsapp", _SRC / "webhooks" / "whatsapp_handlers.py"),
    ("webrtc", _SRC / "api" / "v1" / "webrtc.py"),
]


def _agent_id_args(path: Path) -> list[str]:
    """Source of the `agent_id=` argument of every CallContext(...) in a file."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "CallContext":
            continue
        for kw in node.keywords:
            if kw.arg == "agent_id":
                out.append(ast.unparse(kw.value))
    return out


@pytest.mark.unit
def test_the_sentinel_is_a_real_uuid() -> None:
    """It is passed where a UUID is declared, and looked up in the database —
    it has to be one, and it must match no agent that exists."""
    assert isinstance(DYNAMIC_AGENT_ID, UUID)
    assert DYNAMIC_AGENT_ID == UUID(int=0)


@pytest.mark.unit
@pytest.mark.parametrize("label,path", VOICE_PATHS, ids=[p[0] for p in VOICE_PATHS])
def test_every_voice_transport_tolerates_a_call_with_no_agent_row(
    label: str, path: Path
) -> None:
    args = _agent_id_args(path)
    assert args, f"{label} ({path.name}): no CallContext(agent_id=...) found — moved?"
    for arg in args:
        assert "DYNAMIC_AGENT_ID" in arg, (
            f"{label} ({path.name}): CallContext(agent_id={arg}) does not fall back "
            "to DYNAMIC_AGENT_ID — a call-init response carrying an inline `agent` "
            "instead of an `agent_id` will crash this transport before the pipeline "
            "starts"
        )


@pytest.mark.unit
@pytest.mark.parametrize("label,path", VOICE_PATHS, ids=[p[0] for p in VOICE_PATHS])
def test_no_transport_reinvents_the_sentinel(label: str, path: Path) -> None:
    """Three placeholders in three files is how one of them ended up being a
    string that was then fed to UUID()."""
    for arg in _agent_id_args(path):
        assert "UUID(int=0)" not in arg, (
            f"{label} ({path.name}): uses its own UUID(int=0) instead of the "
            "shared DYNAMIC_AGENT_ID"
        )
