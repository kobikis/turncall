"""Every live call takes a slot, and every one that runs holds it (#224).

The live-call limit is only as good as its coverage: a voice entry point that
writes a call row without taking a slot is uncounted, and one that runs a call
without `hold_live_call` keeps its slot until the lease lapses — every call, a
project short of a slot for two minutes after each hangup. Both are silent, so
this is structural, like `test_ambience_wiring.py`: it fails on the transport
someone adds next, not only on the four there are today. The behaviour itself
is `tests/integration/test_live_call_limit.py`'s.
"""

import ast
import inspect
from pathlib import Path

import pytest

import turncall
from turncall.orchestrator import pipeline_builder

pytestmark = pytest.mark.unit

SRC = Path(turncall.__file__).parent
ADMISSIONS = {"admit_inbound_call", "admit_live_call"}
# An eval is not a call: no row, no slot (ADR-0018).
NOT_A_CALL = ("evals/",)


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(SRC)), ast.parse(path.read_text()))
        for path in sorted(SRC.rglob("*.py"))
        if not str(path.relative_to(SRC)).startswith(NOT_A_CALL)
    ]


def _calling(name: str) -> dict[str, ast.Module]:
    return {
        rel: tree
        for rel, tree in _modules()
        if any(_name(c) == name for c in _calls(tree))
    }


def test_every_call_row_is_created_under_a_slot():
    creators = _calling("create_call")
    assert len(creators) >= 4, creators  # Twilio, outbound, WebRTC, WhatsApp
    for rel, tree in creators.items():
        names = {_name(c) for c in _calls(tree)}
        assert names & ADMISSIONS, f"{rel} writes a call row without taking a slot"
        for call in (c for c in _calls(tree) if _name(c) == "create_call"):
            keywords = {k.arg for k in call.keywords}
            assert "call_id" in keywords, (
                f"{rel}: the row must carry the id its slot was taken under"
            )


def test_every_call_that_runs_holds_its_slot():
    runners = _calling("build_call_pipeline") | _calling("start_call_pipeline")
    for rel, tree in runners.items():
        if rel == "orchestrator/pipeline_builder.py":
            continue
        uses_start = any(_name(c) == "start_call_pipeline" for c in _calls(tree))
        holds = any(_name(c) == "hold_live_call" for c in _calls(tree))
        assert uses_start or holds, f"{rel} runs a call without holding its slot"
    assert "hold_live_call" in inspect.getsource(pipeline_builder.start_call_pipeline)


async def test_without_redis_a_call_is_admitted(monkeypatch):
    """A limit is a guardrail: Redis being down must not take the line down."""
    import uuid

    from turncall.services import limits

    def _down():
        raise RuntimeError("Redis not initialized")

    monkeypatch.setattr(limits, "get_redis", _down)
    assert await limits.claim_live_call(uuid.uuid4(), uuid.uuid4(), 1)
    await limits.release_live_call(uuid.uuid4(), uuid.uuid4())  # never raises
