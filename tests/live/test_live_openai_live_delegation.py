"""gpt-live-1 delegates, over the eval loopback (#243, #244).

Responses mode first, then client mode — the agent's own llm as the delegate,
the default for an `openai_live` agent with tools.

The live model calls no tools of its own; in responses mode the OpenAI-hosted
delegate makes the function calls and the live service runs them with
TurnCall's handlers. This drives an `openai_live` agent with an
`s2s.delegation` block and asserts the delegate's calls reached TurnCall's
mocks — a mocked lookup the caller asks for.

`end_call` on goodbye is deliberately not asserted. gpt-live-1 decides by
itself whether to delegate, and measured against this script it said goodbye
without delegating the hangup about half the time — a model limit recorded on
#242, not wiring this test can pin. That a delegated `end_call` arms the
hangup gate in the call's pipeline is pinned hermetically in
`tests/unit/test_openai_live_delegation.py`.

Audio modality, not text: gpt-live-1 answers what it *hears*. In text mode the
caller's line reaches it only as prior session context, and it sat silent
until the session expired. The caller is spoken by Kokoro (local, downloaded
on first use).

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
        "first_message": "Thanks for calling Luigi's, how can I help?",
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
            # A caller waits for the greeting; the live session starts ~2s
            # after connect, and audio heard before that is dropped.
            {
                "user": "Hello?",
                "expect": [
                    {
                        "event": "response",
                        "text_contains": "calling",
                        "within_ms": 30000,
                    }
                ],
            },
            {
                "user": "Hi, what time do you open today?",
                "expect": [
                    {
                        "event": "function_call",
                        "calls": [{"name": "check_hours"}],
                        "within_ms": 60000,
                    },
                ],
            },
        ]
    }
    mocks = ToolMocks(
        responses={
            "check_hours": {"today": "12:00-22:00"},
            # Still mocked: the agent lists it, and an unmocked call errors
            # the iteration under mock_only.
            "end_call": {"success": True, "message": "ok", "details": None},
        }
    )
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.AUDIO),
        name="live-openai-live-delegation",
    )
    agent_errors: list[str] = []
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.AUDIO,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=mocks,
        agent_errors=agent_errors,
    )

    called = [c["tool_name"] for c in mocks.calls]
    assert "check_hours" in called, (called, agent_errors)
    assert not mocks.refused
    assert result.passed, ([str(f) for f in result.failures], agent_errors)


async def test_client_mode_delegates_a_mocked_tool_to_the_agents_llm(
    session_factory: Any,
) -> None:
    """No delegation block: tools make it client mode, the agent's own llm.

    Asserted on the mocks rather than a `function_call` expectation: the
    delegate runs in its own worker, outside the pipeline the harness
    observes. The answer is what gpt-live-1 relays back from the delegate.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY")
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "s2s": {"provider": "openai_live"},
        "tools": [
            {
                "name": "check_hours",
                "description": "Look up the restaurant's opening hours.",
                "parameters_schema": _EMPTY,
                "webhook_url": "https://example.invalid/hours",
            }
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
                "user": "Hello?",
                "expect": [
                    {
                        "event": "response",
                        "text_contains": "calling",
                        "within_ms": 30000,
                    }
                ],
            },
            {
                "user": "Hi, what time do you open today?",
                # Waits for the relayed answer; its wording ("noon", "12 PM")
                # is gpt-live-1's, so the assertion is on the mocks below.
                "expect": [{"event": "response", "within_ms": 60000}],
            },
        ]
    }
    mocks = ToolMocks(responses={"check_hours": {"today": "12:00-22:00"}})
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.AUDIO),
        name="live-openai-live-client-delegation",
    )
    agent_errors: list[str] = []
    await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.AUDIO,
        settings=Settings(),
        session_factory=session_factory,
        run_id=uuid4(),
        tool_mocks=mocks,
        agent_errors=agent_errors,
    )

    called = [c["tool_name"] for c in mocks.calls]
    assert called and set(called) == {"check_hours"}, (called, agent_errors)
    assert not mocks.refused
