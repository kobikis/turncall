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
from turncall.orchestrator import delegation_events
from turncall.services.tool_mocks import ToolMocks

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_EMPTY = {"type": "object", "properties": {}}


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `delegation.completed` payload (#247). An eval stores none, so
    they are caught where they are made."""
    seen: list[dict[str, Any]] = []
    make = delegation_events.delegation_payload

    def _capture(*args: Any) -> dict[str, Any]:
        seen.append(make(*args))
        return seen[-1]

    monkeypatch.setattr(delegation_events, "delegation_payload", _capture)
    return seen


@pytest.fixture
def session_factory() -> Any:
    from turncall.storage.database import create_engine, create_session_factory

    return create_session_factory(create_engine(Settings().database))


async def test_a_mocked_tool_and_end_call_are_delegated(
    session_factory: Any, outcomes: list[dict[str, Any]]
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
            # The agent speaks first (no `user:`). A "Hello?" here used to be
            # dropped before the session started; since #256 it is heard, over
            # the greeting, and the greeting no longer counts as its answer.
            {
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
            # Time for the delegation to finish: it ends with the response
            # after the function call, past the expectation above (#247).
            {
                "user": "Great, thank you.",
                "expect": [{"event": "response", "within_ms": 60000}],
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
    assert outcomes, "no delegation.completed: gpt-live-1 never delegated"
    assert {"mode": "responses", "outcome": "answered"}.items() <= outcomes[
        0
    ].items(), outcomes


async def test_client_mode_delegates_a_mocked_tool_to_the_agents_llm(
    session_factory: Any, outcomes: list[dict[str, Any]]
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
    assert outcomes, "no delegation.completed: gpt-live-1 never delegated"
    assert {"mode": "client", "outcome": "answered"}.items() <= outcomes[0].items(), (
        outcomes
    )


async def test_a_client_delegate_with_a_rejected_key_fails_over(
    session_factory: Any, outcomes: list[dict[str, Any]]
) -> None:
    """#246: the delegate's primary has a key OpenAI rejects (401, unusable),
    so the delegation moves to the backup and is still answered."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY")
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "s2s": {"provider": "openai_live"},
        "llm": {
            "provider": "openai",
            "api_key": "sk-turncall-rejected",
            "failover": [{"provider": "openai", "model": "gpt-4.1-mini"}],
        },
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
                "expect": [{"event": "response", "within_ms": 60000}],
            },
        ]
    }
    mocks = ToolMocks(responses={"check_hours": {"today": "12:00-22:00"}})
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.AUDIO),
        name="live-openai-live-delegate-failover",
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

    assert any(
        "llm failed over from openai/gpt-4o-mini to openai/gpt-4.1-mini" in e
        for e in agent_errors
    ), agent_errors
    assert [c["tool_name"] for c in mocks.calls] == ["check_hours"], agent_errors
    assert outcomes, "no delegation.completed: gpt-live-1 never delegated"
    assert {"mode": "client", "outcome": "answered"}.items() <= outcomes[0].items(), (
        outcomes
    )


async def test_a_tool_mode_kb_is_delegated_and_its_fact_relayed(
    session_factory: Any,
    outcomes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#254: a tool-mode KB, and no other tools, gets a client delegate whose
    `query_knowledge` answers — and gpt-live-1 relays what it found.

    The inline target names the KB itself (#260), resolved against a real
    project. Retrieval is stubbed: what is under test is the delegation, not
    pgvector. The fact is one nothing but the stub could have supplied.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY")
    from turncall.orchestrator import knowledge_processor
    from turncall.services.retrieval import RetrievalResult, RetrievedChunk
    from turncall.storage.repositories import knowledge_repo, project_repo

    async with session_factory() as db:
        project = await project_repo.create_project(db, name="live-kb-260")
        kb = await knowledge_repo.create_knowledge_base(
            db, project_id=project.id, name="menu"
        )
        await db.commit()
    kb_id = kb.id
    queries: list[str] = []

    async def _retrieve(session: Any, *, query: str, **_: Any) -> RetrievalResult:
        queries.append(query)
        chunk = RetrievedChunk(
            chunk_id=uuid4(),
            document_id=uuid4(),
            content="This week's house special is saffron risotto.",
            similarity=0.9,
            chunk_index=0,
            token_count=9,
            filename="menu.txt",
        )
        return RetrievalResult(chunks=[chunk], query=query, knowledge_base_ids=[kb_id])

    monkeypatch.setattr(knowledge_processor, "retrieve", _retrieve)
    blob = {
        "pipeline_mode": "s2s",
        "system_prompt": "You are the receptionist at Luigi's restaurant.",
        "first_message": "Thanks for calling Luigi's, how can I help?",
        "s2s": {"provider": "openai_live"},
        "knowledge_bases": [{"knowledge_base_id": str(kb_id), "mode": "tool"}],
    }
    target = ResolvedTarget(
        project_id=project.id,
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )
    definition = {
        "turns": [
            {
                "expect": [
                    {
                        "event": "response",
                        "text_contains": "calling",
                        "within_ms": 30000,
                    }
                ],
            },
            {
                "user": "Hi, what is this week's house special?",
                "expect": [{"event": "response", "within_ms": 60000}],
            },
            # The relayed answer follows the acknowledgement. No `user:`: a
            # caller line here was spoken over the relay and lost it.
            {"expect": [{"event": "response", "within_ms": 60000}]},
        ]
    }
    parsed = scenario_mod.parse(
        scenario_mod.with_modality(definition, EvalModality.AUDIO),
        name="live-openai-live-kb-tool",
    )
    agent_errors: list[str] = []
    try:
        result = await run_iteration(
            parsed=parsed,
            kind=EvalKind.SCRIPTED,
            target=target,
            modality=EvalModality.AUDIO,
            settings=Settings(),
            session_factory=session_factory,
            run_id=uuid4(),
            tool_mocks=ToolMocks(responses={}),
            agent_errors=agent_errors,
        )
    finally:
        async with session_factory() as db:
            await project_repo.hard_delete_project(db, project.id)
            await db.commit()

    heard = " ".join(
        e.get("text", "") for e in result.events_seen if e["type"] == "response"
    )
    assert queries, ("query_knowledge never ran", heard, agent_errors)
    assert "saffron" in heard.lower(), (heard, queries, agent_errors)
    assert outcomes and outcomes[0]["mode"] == "client", outcomes
