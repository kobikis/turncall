"""An eval run's skills (#431): resolved once, recorded, run as recorded.

The run resolves its target's skills before the first iteration, so every
iteration runs the same versions (pinned) and `resolved_config` names them.
`load_skill` is never mocked; a mock for it is ignored and said so, and a
mock for a resolved skill's tool is one the run can call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, SkillRef, ToolDefinition
from turncall.evals import skills as eval_skills
from turncall.evals.run_warnings import warn_never_mocked, warn_unmatched_mocks
from turncall.evals.targets import ResolvedTarget
from turncall.orchestrator import tool_bridge
from turncall.services.skills import ResolvedSkill
from turncall.services.tool_mocks import ToolMocks, intercept

pytestmark = pytest.mark.unit


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters_schema={"type": "object", "properties": {}},
        webhook_url=f"https://x.example/{name}",
    )


def _skill(name: str, *tools: str, version: int = 1) -> ResolvedSkill:
    return ResolvedSkill(
        id=uuid4(),
        name=name,
        version=version,
        description=name,
        instructions=name,
        tools=tuple(_tool(t) for t in tools),
    )


def _target(**config) -> ResolvedTarget:
    blob = {"system_prompt": "hi", **config}
    return ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
    )


async def _resolve(target: ResolvedTarget, resolved: tuple[ResolvedSkill, ...]):
    with patch.object(
        eval_skills, "resolve_skills", AsyncMock(return_value=resolved)
    ) as resolve:
        return await eval_skills.resolve_run_skills(object(), target), resolve


class TestResolvedOnceForTheRun:
    async def test_an_agent_without_skills_is_left_alone(self) -> None:
        target = _target()
        run, resolve = await _resolve(target, ())

        assert run.target is target
        assert run.warnings == []
        assert run.snapshot({"system_prompt": "hi"}) == {"system_prompt": "hi"}
        resolve.assert_not_awaited()

    async def test_the_resolved_versions_are_recorded_and_pinned(self) -> None:
        target = _target(skills=[{"skill": "explain_bill"}, {"skill": "gone"}])
        bill = _skill("explain_bill", "get_invoices", version=3)
        run, resolve = await _resolve(target, (bill,))

        # In the run's project, with no variables: an eval has no call row.
        assert resolve.await_args.args[1] == target.project_id
        assert resolve.await_args.kwargs["variables"] == {}
        assert run.snapshot({"system_prompt": "hi"}) == {
            "system_prompt": "hi",
            "resolved_skills": [
                {
                    "id": str(bill.id),
                    "skill": "explain_bill",
                    "version": 3,
                    "pinned": False,
                }
            ],
        }
        # Every iteration runs that version, and only what resolved.
        assert run.target.config.skills == [
            SkillRef(skill="explain_bill", version_id=bill.id)
        ]
        assert target.config.skills[0].version_id is None  # the original kept
        assert run.tool_names == {"get_invoices"}
        [warning] = run.warnings
        assert warning["code"] == "skills_skipped"
        assert warning["skills"] == ["gone"]

    async def test_nothing_resolved_records_an_empty_resolution(self) -> None:
        run, _ = await _resolve(_target(skills=[{"skill": "gone"}]), ())

        assert run.snapshot({})["resolved_skills"] == []
        assert run.target.config.skills == []

    async def test_a_resolution_that_raises_runs_without_skills(self) -> None:
        target = _target(skills=[{"skill": "explain_bill"}])
        with patch.object(
            eval_skills, "resolve_skills", AsyncMock(side_effect=RuntimeError("db"))
        ):
            run = await eval_skills.resolve_run_skills(object(), target)

        assert run.target.config.skills == []
        assert run.warnings[0]["skills"] == ["explain_bill"]

    async def test_an_s2s_agent_resolves_none_and_says_why(self) -> None:
        target = _target(pipeline_mode="s2s", skills=[{"skill": "explain_bill"}])
        run, resolve = await _resolve(target, (_skill("explain_bill"),))

        resolve.assert_not_awaited()
        assert run.target.config.skills == []
        assert run.warnings[0]["code"] == "skills_ignored_on_s2s"
        assert run.snapshot({})["resolved_skills"] == []


class TestLoadSkillIsNeverMocked:
    def test_intercept_lets_it_run_whatever_the_mocks_say(self) -> None:
        mocks = ToolMocks(responses={"load_skill": {"success": True}})

        assert intercept(mocks, "load_skill", {"name": "x"}) is None
        assert intercept(ToolMocks(), "load_skill", {"name": "x"}) is None
        assert mocks.refused == [] and mocks.calls == []

    def test_a_skill_tool_still_fails_closed(self) -> None:
        mocks = ToolMocks()

        assert json.loads(intercept(mocks, "get_invoices", {})) == {
            "error": "unmocked tool: get_invoices"
        }
        assert mocks.refused == ["get_invoices"]

    async def test_it_runs_under_mock_only_and_is_recorded_as_applied(self) -> None:
        class _LLM:
            def __init__(self) -> None:
                self.handlers: dict = {}

            def register_function(self, name, fn, **_options) -> None:
                self.handlers[name] = fn

        mocks = ToolMocks()
        context = SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            mcp_manager=None,
            tool_mocks=mocks,
            is_eval=True,
            records=False,
            has_database=True,
        )
        llm = _LLM()
        loaded = '{"success": true, "message": "explain_bill is loaded"}'
        tool_bridge.register_tools(
            llm,
            [ToolDefinition(name="load_skill", description="d", parameters_schema={})],
            context,
        )
        with patch(
            "turncall.orchestrator.skill_loader.run_load_skill",
            AsyncMock(return_value=loaded),
        ) as run_load:
            await llm.handlers["load_skill"](
                SimpleNamespace(
                    function_name="load_skill",
                    arguments={"name": "explain_bill"},
                    result_callback=AsyncMock(),
                )
            )

        run_load.assert_awaited_once()
        assert mocks.refused == []
        assert mocks.calls == [
            {
                "tool_name": "load_skill",
                "arguments": {"name": "explain_bill"},
                "result": loaded,
                "outcome": "applied",
            }
        ]

    def test_a_mock_for_it_is_ignored_and_the_run_says_so(self) -> None:
        [warning] = warn_never_mocked({"load_skill": {}, "get_invoices": {}})

        assert warning["code"] == "mock_ignored"
        assert warning["tools"] == ["load_skill"]
        assert warn_never_mocked({"get_invoices": {}}) == []


def test_a_mock_for_a_resolved_skills_tool_can_fire() -> None:
    target = _target()
    mocks = {"get_invoices": {}, "typo": {}}

    [warning] = warn_unmatched_mocks(
        mocks, target, run_id=uuid4(), skill_tools={"get_invoices"}
    )
    assert warning["tools"] == ["typo"]
