"""Skills in an eval run (#431), over the real runner, harness and pipeline.

A run resolves its target's skills once, in the run's project — an inline
target's too — and records the versions in `resolved_config`. `load_skill` is
never mocked: it changes the agent under test, so it runs under `mock_only`
and a `function_call` expectation on it passes when the agent loads the skill
and fails when it does not. The skill's own tools are mocked, and refused
unmocked, like any tool. Only the providers are stubbed (the harness's stub
LLM, which really calls tools); the database, the worker's runner, the eval
loopback and pipecat's harness are the real ones.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.harness.stubs import Given, Script, ToolCall, install_stub_providers
from turncall.config.settings import Settings
from turncall.domain.enums import EvalRunStatus
from turncall.evals.runner import execute_run
from turncall.orchestrator import pipeline_builder
from turncall.services.credentials import Credentials
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import ProjectRow, SkillRow
from turncall.storage.repositories import eval_repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

QUESTION = "Why did my bill go up?"
ANSWER = "It went up by twelve dollars: a tariff change."
SCENARIO = {
    "turns": [
        {
            "user": QUESTION,
            "expect": [
                {
                    "event": "function_call",
                    "calls": [{"name": "load_skill", "args": {"name": "explain_bill"}}],
                    # The failing case waits this long, not pipecat's default.
                    "within_ms": 10000,
                }
            ],
        }
    ]
}
INVOICES = {"last": 52, "previous": 40}


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


def _agent(*, loads: bool):
    """The model: asked about a bill, it loads `explain_bill` (when `loads`),
    then calls the skill's `get_invoices` and answers from it."""

    def answer(given: Given) -> Any:
        last = given[-1]
        if last["role"] == "tool":
            if "is loaded" in str(last.get("content")):
                return ToolCall("get_invoices", {})
            return ANSWER
        if loads and "load_skill" in given.tools and "bill" in str(last["content"]):
            return ToolCall("load_skill", {"name": "explain_bill"})
        return "Hello, how can I help?"

    return answer


async def _run(
    factory,
    monkeypatch,
    *,
    loads: bool,
    mocks: dict[str, Any],
    in_library: bool = False,
) -> tuple[Any, SkillRow]:
    """`in_library`: the skill is held by a skill library the run's project
    is linked to (#433), not by the project itself."""
    install_stub_providers(
        Script(agent=(_agent(loads=loads),) * 12), monkeypatch.setattr
    )
    monkeypatch.setattr(
        pipeline_builder, "load_call_credentials", AsyncMock(return_value=Credentials())
    )
    async with factory() as session:
        project = ProjectRow(name=f"eval-skills-{uuid4().hex[:8]}")
        holder = project
        if in_library:
            holder = ProjectRow(name=f"eval-skills-library-{uuid4().hex[:8]}")
            session.add(holder)
            await session.flush()
            project.skill_library_id = holder.id
        session.add(project)
        await session.flush()
        skill = SkillRow(
            project_id=holder.id,
            name="explain_bill",
            version=1,
            state="published",
            description="Use when the caller asks why a bill changed.",
            instructions="Compare the last two invoices.",
            tools=[
                {
                    "name": "get_invoices",
                    "description": "The last two invoices",
                    "parameters_schema": {"type": "object", "properties": {}},
                    "webhook_url": "https://billing.example/invoices",
                }
            ],
            knowledge_bases=[],
        )
        session.add(skill)
        inline = {
            "system_prompt": "You are the support agent.",
            "first_message": None,
            # `vip_retention` is no skill of the project's: skipped, said so.
            "skills": [{"skill": "explain_bill"}, {"skill": "vip_retention"}],
        }
        run = await eval_repo.create_run(
            session,
            project_id=project.id,
            scenario_id=None,
            scenario_name="loads-the-bill-skill",
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
        return await eval_repo.get_run(session, run_id), skill


def _codes(run: Any) -> dict[str, dict[str, Any]]:
    return {w["code"]: w for w in run.warnings}


async def test_a_run_that_loads_the_skill_passes_and_records_its_version(
    factory, monkeypatch
):
    run, skill = await _run(
        factory,
        monkeypatch,
        loads=True,
        mocks={"get_invoices": INVOICES, "load_skill": {"success": True}},
    )

    assert run.status == EvalRunStatus.PASSED.value, (run.error, run.results)
    assert run.resolved_config["resolved_skills"] == [
        {
            "id": str(skill.id),
            "skill": "explain_bill",
            "version": 1,
            "pinned": False,
        }
    ]
    # `load_skill` ran though a mock named it; the skill's tool was mocked.
    [entry] = run.results
    outcomes = [(c["tool_name"], c["outcome"]) for c in entry["tool_calls"]]
    assert outcomes == [("load_skill", "applied"), ("get_invoices", "mocked")]

    warnings = _codes(run)
    assert warnings["skills_skipped"]["skills"] == ["vip_retention"]
    assert warnings["mock_ignored"]["tools"] == ["load_skill"]
    # A skill's tool is one the run can call: its mock is not unmatched.
    assert "mock_matches_no_tool" not in warnings


async def test_a_run_in_a_linked_project_records_the_librarys_versions(
    factory, monkeypatch
):
    """#433: the run's project reads its skills from its library, so the
    run resolves, records and loads the library's version."""
    run, skill = await _run(
        factory,
        monkeypatch,
        loads=True,
        mocks={"get_invoices": INVOICES},
        in_library=True,
    )

    assert run.status == EvalRunStatus.PASSED.value, (run.error, run.results)
    assert run.resolved_config["resolved_skills"] == [
        {"id": str(skill.id), "skill": "explain_bill", "version": 1, "pinned": False}
    ]
    [entry] = run.results
    outcomes = [(c["tool_name"], c["outcome"]) for c in entry["tool_calls"]]
    assert outcomes == [("load_skill", "applied"), ("get_invoices", "mocked")]


async def test_a_run_whose_agent_never_loads_the_skill_fails(factory, monkeypatch):
    run, _skill = await _run(
        factory, monkeypatch, loads=False, mocks={"get_invoices": INVOICES}
    )

    assert run.status == EvalRunStatus.FAILED.value, (run.error, run.results)
    [entry] = run.results
    assert entry["tool_calls"] == []


async def test_an_unmocked_skill_tool_is_refused_like_any_tool(factory, monkeypatch):
    run, _skill = await _run(factory, monkeypatch, loads=True, mocks={})

    assert run.status == EvalRunStatus.ERRORED.value, run.results
    assert run.error == "unmocked tool: get_invoices"
