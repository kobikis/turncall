"""The step-tool reminder on a live call (#417): `end_call` and `load_skill`
hand back their own result from inside the handler's branch, so the reminder
rides on the result callback itself (`tool_bridge._with_followup`)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.domain.procedure_state import ProcedureState
from turncall.orchestrator import tool_bridge
from turncall.orchestrator.procedures import CallProcedures
from turncall.orchestrator.skill_loader import CallSkills
from turncall.services.skills import ResolvedSkill

pytestmark = pytest.mark.unit

REMINDER = "\n\n[Procedure] 'wrap_up' is at step 'wrap', whose tool"


class _LLM:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register_function(self, name, fn, **_options) -> None:
        self.handlers[name] = fn


def _wrap_up(tool: str) -> dict:
    return {
        "name": "wrap_up",
        "start": "call",
        "steps": [
            {
                "id": "wrap",
                "kind": "instruct",
                "start": True,
                "tool": tool,
                "text": "Wrap the call up.",
                "moves": [{"to": "done", "when": "the tool answered"}],
            },
            {"id": "done", "kind": "end", "text": "Say goodbye."},
        ],
    }


async def _call_tool(
    tool: str, arguments: dict | None = None, skills: CallSkills | None = None
) -> AsyncMock:
    """Run `tool` through `register_tools`' handler on a live call whose
    procedure is at the step `tool` belongs to; the result callback."""
    config = AgentConfig(system_prompt="s", procedures=[_wrap_up(tool)])
    context = SimpleNamespace(
        call_id=uuid4(),
        project_id=uuid4(),
        mcp_manager=None,
        tool_mocks=None,
        is_eval=False,
        records=False,
        has_database=True,
        skills=skills,
        session_factory=MagicMock(),
        procedures=CallProcedures(ProcedureState.at_call_start(config.procedures)),
    )
    llm = _LLM()
    tool_bridge.register_tools(
        llm, [ToolDefinition(name=tool, description="d", parameters_schema={})], context
    )
    answer = AsyncMock()

    async def queue_frame(_frame) -> None:
        return None

    await llm.handlers[tool](
        SimpleNamespace(
            function_name=tool,
            tool_call_id="1",
            arguments=arguments or {},
            llm=MagicMock(push_frame=AsyncMock()),
            context=MagicMock(),
            pipeline_worker=SimpleNamespace(queue_frame=queue_frame),
            result_callback=answer,
        )
    )
    return answer


async def test_end_call_on_the_live_shielded_path_gets_it(monkeypatch) -> None:
    async def builtin(*_args):
        return '{"success": true, "message": "ok", "details": null}'

    monkeypatch.setattr(tool_bridge, "_execute_builtin", builtin)

    answer = await _call_tool("end_call")

    assert REMINDER in answer.await_args.args[0]


async def test_a_real_skill_load_gets_it_and_keeps_its_properties() -> None:
    skill = ResolvedSkill(
        id=uuid4(),
        name="explain_bill",
        version=1,
        description="When a bill needs explaining.",
        instructions="Explain the bill.",
        tools=(),
    )
    skills = CallSkills(
        AgentConfig(system_prompt="s"), (skill,), tool_bridge.SwitchInputs()
    )

    answer = await _call_tool("load_skill", {"name": "explain_bill"}, skills)

    assert REMINDER in answer.await_args.args[0]
    # `run_load_skill`'s own delivery: the next inference waits for the switch.
    assert answer.await_args.kwargs["properties"].run_llm is False
