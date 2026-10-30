"""What a reported move leaves on the call's event log (#443).

An accepted move is a `procedure.step` `{procedure, from, to, kind}`, a
refused one a `procedure.refused` `{procedure, from, to}`, stored only and
off the tool's path — in the order the moves were made, however long each
write takes. Nothing for a call with no row (an eval). The harness test
(`tests/integration/test_harness_call_procedure_record.py`) proves it on a
real call; this one the order when writes overlap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from turncall.domain.procedure_state import ProcedureState
from turncall.domain.procedures import Procedure
from turncall.orchestrator import procedures
from turncall.orchestrator.procedures import CallProcedures, run_move_to_step

pytestmark = pytest.mark.unit

ROUTER = Procedure.model_validate(
    {
        "name": "router",
        "start": "call",
        "steps": [
            {
                "id": "lights",
                "kind": "ask",
                "start": True,
                "text": "Ask about the lights.",
                "moves": [{"to": "restart", "when": "the light is red"}],
            },
            {
                "id": "restart",
                "kind": "instruct",
                "text": "Restart it.",
                "moves": [{"to": "done", "when": "it works"}],
            },
            {"id": "done", "kind": "end", "text": "Close."},
        ],
    }
)


def _context(*, records: bool = True) -> SimpleNamespace:
    def no_session() -> Any:
        raise AssertionError("an eval has no call row to write to")

    return SimpleNamespace(
        records=records,
        call_id=uuid4(),
        project_id=uuid4(),
        session_factory=no_session,
        procedures=CallProcedures(ProcedureState.at_call_start([ROUTER])),
    )


def _moves(ctx: Any, *targets: str) -> None:
    for to in targets:
        run_move_to_step({"procedure": "router", "to": to}, ctx)


async def _settled() -> None:
    for _ in range(50):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


async def test_moves_are_stored_in_the_order_they_were_made(monkeypatch) -> None:
    stored: list[tuple[str, dict[str, Any]]] = []
    delays = iter([0.03, 0.0, 0.0])

    async def store(_ctx: Any, event_type: str, payload: dict[str, Any]) -> None:
        # The first write is the slowest: it must still come first.
        await asyncio.sleep(next(delays))
        stored.append((str(event_type), payload))

    monkeypatch.setattr(procedures, "store_call_event", store)
    ctx = _context()

    _moves(ctx, "restart", "lights_typo", "done")
    await _settled()

    assert stored == [
        (
            "procedure.step",
            {
                "procedure": "router",
                "from": "lights",
                "to": "restart",
                "kind": "instruct",
            },
        ),
        (
            "procedure.refused",
            {"procedure": "router", "from": "restart", "to": "lights_typo"},
        ),
        (
            "procedure.step",
            {"procedure": "router", "from": "restart", "to": "done", "kind": "end"},
        ),
    ]


async def test_an_eval_stores_nothing(monkeypatch) -> None:
    stored: list[str] = []

    async def store(_ctx: Any, event_type: str, _payload: dict[str, Any]) -> None:
        stored.append(event_type)

    monkeypatch.setattr(procedures, "store_call_event", store)
    ctx = _context(records=False)

    _moves(ctx, "restart", "nowhere")
    await _settled()

    assert stored == []
    # The moves themselves still apply: the eval follows the procedure.
    assert ctx.procedures.state.run("router").current == "restart"


async def test_staying_on_a_step_stores_nothing(monkeypatch) -> None:
    """A move to the current step is the model staying put: neither a step
    in the path nor a refused move."""
    stored: list[str] = []

    async def store(_ctx: Any, event_type: str, _payload: dict[str, Any]) -> None:
        stored.append(str(event_type))

    monkeypatch.setattr(procedures, "store_call_event", store)
    ctx = _context()

    _moves(ctx, "lights", "restart")
    await _settled()

    assert stored == ["procedure.step"]
    assert ctx.procedures.state.run("router").path == ("lights", "restart")
