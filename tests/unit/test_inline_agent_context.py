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


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"{path.name}: no function named {name}")


@pytest.mark.unit
def test_the_inbound_webhook_never_dereferences_a_possibly_absent_agent() -> None:
    """`agent` is None whenever call-init returned an inline config, and the
    handler runs on past that. A bare `agent.id` in a *log line* is what crashed
    every inbound call with integrations attached — the resolved
    `agent_id_for_call` is the safe form, and `agent.name if agent else ...` the
    safe idiom where the object itself is wanted.
    """
    fn = _function(_SRC / "webhooks" / "twilio_handlers.py", "inbound_voice_webhook")
    bare = [
        f"agent.{node.attr}"
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "agent"
        # `agent.x if agent else y` guards itself.
        and not any(
            isinstance(p, ast.IfExp)
            for p in ast.walk(fn)
            if isinstance(p, ast.IfExp) and node in ast.walk(p)
        )
    ]
    assert bare == [], (
        f"inbound_voice_webhook dereferences {bare} unguarded — a call running an "
        "inline agent has no agent row and this crashes the webhook"
    )


# --- the config the call ran with has to survive the call ---------------------
#
# An inline agent leaves no agent row, so the only record of what the call ran
# with is `metadata_json["dynamic_config"]`. Everything after the hangup reads it
# back through `config_for_call`: post-call analysis, and — because the analysis
# trigger is what dispatches `call.ended` — the webhook itself, and therefore
# every end-of-call Automation hanging off it. Twilio and WhatsApp store it.
# WebRTC resolved it, used it, and dropped it, so a browser call with an inline
# agent finalized silently and no `call.ended` ever went out.


def _metadata_keys(path: Path) -> set[str]:
    """Every string key of every dict literal in a file. Blunt on purpose: the
    three transports assemble their metadata differently (inline `**{...} if`
    spreads, separate locals) and the invariant is only that the key is written
    somewhere on the path that builds it."""
    tree = ast.parse(path.read_text())
    return {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


# Not VOICE_PATHS: the file that builds the CallContext is not always the one
# that creates the call row. Twilio answers the inbound webhook in one request
# and opens the media stream in another.
CALL_ROW_PATHS = [
    ("twilio", _SRC / "webhooks" / "twilio_handlers.py"),
    ("whatsapp", _SRC / "webhooks" / "whatsapp_handlers.py"),
    ("webrtc", _SRC / "api" / "v1" / "webrtc.py"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "label,path", CALL_ROW_PATHS, ids=[p[0] for p in CALL_ROW_PATHS]
)
def test_every_transport_persists_the_inline_config(label: str, path: Path) -> None:
    src = path.read_text()
    if "dynamic_config" not in src:
        pytest.skip(f"{label} does not resolve an inline agent")
    assert "dynamic_config" in _metadata_keys(path), (
        f"{label} resolves an inline agent config but never stores it under "
        '"dynamic_config" — the call finalizes with nothing to analyse and '
        "dispatches no call.ended."
    )
