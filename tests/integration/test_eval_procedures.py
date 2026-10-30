"""Procedures in an eval run (#449), over the real runner, harness and pipeline.

A move is a tool call, so a scripted scenario asserts the path with the
`function_call` expectation it already has. `move_to_step` is never mocked:
it runs under `mock_only`, the procedure itself accepts or refuses it, and
the iteration's `tool_calls` show the path taken, each `applied`. An agent
that follows the procedure passes; one that skips the confirmation step fails
— a verdict, not an error. An eval records no `procedure.*` call events. Only
the providers are stubbed (the harness's stub LLM, which really calls tools);
the database, the worker's runner, the eval loopback and pipecat's harness are
the real ones.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tests.harness.stubs import Given, Script, ToolCall, install_stub_providers
from turncall.config.settings import Settings
from turncall.domain.enums import EvalRunStatus
from turncall.evals.runner import execute_run
from turncall.orchestrator import pipeline_builder
from turncall.services.credentials import Credentials
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import CallEventRow, ProjectRow
from turncall.storage.repositories import eval_repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CANCEL_ORDER = {
    "name": "cancel_order",
    "start": "call",
    "steps": [
        {
            "id": "identify",
            "kind": "ask",
            "start": True,
            "text": "Ask for the order number.",
            "moves": [{"to": "confirm", "when": "the caller gave the number"}],
        },
        {
            "id": "confirm",
            "kind": "ask",
            "text": "Read the order back and ask the caller to confirm.",
            "moves": [{"to": "cancelled", "when": "the caller confirms"}],
        },
        {"id": "cancelled", "kind": "end", "text": "Say the order is cancelled."},
    ],
}


def _expect_move(to: str) -> dict[str, Any]:
    return {
        "event": "function_call",
        "calls": [
            {"name": "move_to_step", "args": {"procedure": "cancel_order", "to": to}}
        ],
        # The failing case waits this long, not pipecat's default.
        "within_ms": 10000,
    }


SCENARIO = {
    "turns": [
        {"user": "Please cancel order 1234.", "expect": [_expect_move("confirm")]},
        {"user": "Yes, cancel it.", "expect": [_expect_move("cancelled")]},
    ]
}


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield session_factory
    await engine.dispose()


def _move(to: str) -> ToolCall:
    return ToolCall("move_to_step", {"procedure": "cancel_order", "to": to})


def _agent(*, confirms: bool):
    """The model: given the order number it moves to `confirm` (or, when not
    `confirms`, straight to `cancelled`); told yes, it moves to `cancelled`."""

    def answer(given: Given) -> Any:
        last = given[-1]
        if last["role"] == "tool":
            return "Done."
        text = str(last.get("content"))
        if "1234" in text:
            return _move("confirm" if confirms else "cancelled")
        if "Yes" in text:
            return _move("cancelled")
        return "Hello, how can I help?"

    return answer


async def _procedure_events(factory) -> int:
    async with factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(CallEventRow)
            .where(CallEventRow.event_type.like("procedure.%"))
        )


async def _run(factory, monkeypatch, *, confirms: bool, mocks: dict[str, Any]) -> Any:
    install_stub_providers(
        Script(agent=(_agent(confirms=confirms),) * 12), monkeypatch.setattr
    )
    monkeypatch.setattr(
        pipeline_builder, "load_call_credentials", AsyncMock(return_value=Credentials())
    )
    async with factory() as session:
        project = ProjectRow(name=f"eval-procedures-{uuid4().hex[:8]}")
        session.add(project)
        await session.flush()
        inline = {
            "system_prompt": "You are the orders agent.",
            "first_message": None,
            "procedures": [CANCEL_ORDER],
        }
        run = await eval_repo.create_run(
            session,
            project_id=project.id,
            scenario_id=None,
            scenario_name="confirms-before-cancelling",
            kind="script",
            target={"type": "inline", "agent": inline},
            resolved_scenario={"definition": SCENARIO, "tool_mocks": mocks},
            modality="text",
            iterations=1,
        )
        await session.commit()
        run_id = run.id

    await execute_run(run_id, session_factory=factory, settings=Settings())

    async with factory() as session:
        return await eval_repo.get_run(session, run_id)


def _path(run: Any) -> list[tuple[str, str]]:
    [entry] = run.results
    return [
        (call["arguments"]["to"], call["outcome"])
        for call in entry["tool_calls"]
        if call["tool_name"] == "move_to_step"
    ]


async def test_an_agent_that_follows_the_procedure_passes(factory, monkeypatch):
    events_before = await _procedure_events(factory)

    run = await _run(
        factory,
        monkeypatch,
        confirms=True,
        # Ignored, and said so: a canned "accepted" would hide a skip.
        mocks={"move_to_step": {"success": True}},
    )

    assert run.status == EvalRunStatus.PASSED.value, (run.error, run.results)
    assert _path(run) == [("confirm", "applied"), ("cancelled", "applied")]
    assert {w["code"]: w for w in run.warnings}["mock_ignored"]["tools"] == [
        "move_to_step"
    ]
    # An eval has no call to hang an event on.
    assert await _procedure_events(factory) == events_before


async def test_an_agent_that_skips_the_confirmation_fails(factory, monkeypatch):
    run = await _run(factory, monkeypatch, confirms=False, mocks={})

    # Failed, not errored: the move ran under `mock_only`, and the procedure
    # refused it, which is a verdict about the agent.
    assert run.status == EvalRunStatus.FAILED.value, (run.error, run.results)
    [entry] = run.results
    [skip, *_] = entry["tool_calls"]
    assert (skip["arguments"]["to"], skip["outcome"]) == ("cancelled", "applied")
    assert '"success": false' in skip["result"]
