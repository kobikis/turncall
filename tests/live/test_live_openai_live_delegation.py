"""gpt-live-1 delegates to a Responses model, over the eval loopback (#243).

The live model calls no tools of its own; in responses mode the OpenAI-hosted
delegate makes the function calls and the live service runs them with
TurnCall's handlers. This drives an `openai_live` agent with an
`s2s.delegation` block and asserts the delegate's calls reached TurnCall's
mocks — a mocked lookup, then `end_call` after the caller says goodbye.

An eval builds no hangup gate (the harness owns an iteration's length, #240),
so what ends the iteration here is the script, not the call; that `end_call`
arms the gate in the call's pipeline is pinned hermetically in
`tests/unit/test_openai_live_delegation.py`.

Live: needs OPENAI_API_KEY. Skipped without it.
"""

import os
from typing import Any
from uuid import uuid4

import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_EMPTY = {"type": "object", "properties": {}}


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_mocked_tool_and_end_call_are_delegated(
    session_factory: Any,
) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY")
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "s2s": {
            "provider": "openai_live",
            "delegation": {"mode": "responses", "model": "gpt-5-mini"},
        },
        "tools": [
            {
                "name": "check_hours",
                "description": "Look up the restaurant's opening hours.",
                "parameters_schema": _EMPTY,
                "webhook_url": "https://example.invalid/hours",
            },
            {
                "name": "end_call",
                "description": "Hang up once the caller has said goodbye.",
                "parameters_schema": _EMPTY,
            },
        ],
    }
    target = ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            {
                "user": "Hi, what time do you open today?",
                "expect": [
                    {
                        "event": "function_call",
                        "calls": [{"name": "check_hours"}],
                        "within_ms": 60000,
                    }
                ],
            },
            {
                "user": "Great, that's all I needed. Goodbye!",
                "expect": [
                    {
                        "event": "function_call",
                        "calls": [{"name": "end_call"}],
                        "within_ms": 60000,
                    }
                ],
            },
        ]
    }
    mocks = ToolMocks(
        responses={
            "check_hours": {"today": "12:00-22:00"},
            "end_call": {"success": True, "message": "ok", "details": None},
        }
    )
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.TEXT),
        name="live-openai-live-delegation",
    )
    agent_errors: list[str] = []
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=mocks,
        agent_errors=agent_errors,
    )

    called = [c["tool_name"] for c in mocks.calls]
    assert "check_hours" in called, (called, agent_errors)
    assert "end_call" in called, (called, agent_errors)
    assert not mocks.refused
    assert result.passed, ([str(f) for f in result.failures], agent_errors)
