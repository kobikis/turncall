"""Procedures in an eval run (#449).

`move_to_step` is never mocked, like `load_skill`: it changes the agent under
test (where it is in its procedure) and reaches nothing outside, so it runs
under `mock_only`, is recorded as `applied`, and a mock for it is ignored and
said so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, ToolDefinition
from turncall.domain.procedure_state import ProcedureState
from turncall.evals.run_warnings import warn_never_mocked
from turncall.orchestrator import skill_loader, tool_bridge
from turncall.orchestrator.procedures import CallProcedures, move_to_step_tool
from turncall.services.tool_mocks import ToolMocks, intercept

pytestmark = pytest.mark.unit

CONFIRM = {
    "name": "cancel_order",
    "start": "call",
    "steps": [
        {
            "id": "identify",
            "kind": "ask",
            "start": True,
            "text": "Ask for the order number.",
            "moves": [{"to": "confirm", "when": "the caller gave it"}],
        },
        {
            "id": "confirm",
            "kind": "ask",
            "text": "Read the order back and ask the caller to confirm.",
            "moves": [{"to": "done", "when": "the caller confirms"}],
        },
        {"id": "done", "kind": "end", "text": "Say it is cancelled."},
    ],
}


class _LLM:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register_function(self, name, fn, **_options) -> None:
        self.handlers[name] = fn


async def _move(llm: _LLM, to: str) -> str:
    callback = AsyncMock()
    await llm.handlers["move_to_step"](
        SimpleNamespace(
            function_name="move_to_step",
            arguments={"procedure": "cancel_order", "to": to},
            result_callback=callback,
        )
    )
    return callback.await_args.args[0]


class TestMoveToStepIsNeverMocked:
    def test_intercept_lets_it_run_whatever_the_mocks_say(self) -> None:
        mocks = ToolMocks(responses={"move_to_step": {"success": True}})

        assert intercept(mocks, "move_to_step", {"to": "x"}) is None
        assert intercept(ToolMocks(), "move_to_step", {"to": "x"}) is None
        assert mocks.refused == [] and mocks.calls == []

    async def test_it_runs_under_mock_only_and_is_recorded_as_applied(self) -> None:
        mocks = ToolMocks(responses={"move_to_step": {"success": True}})
        config = AgentConfig(system_prompt="s", procedures=[CONFIRM])
        holder = CallProcedures(ProcedureState.at_call_start(config.procedures))
        context = SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            mcp_manager=None,
            tool_mocks=mocks,
            is_eval=True,
            records=False,
            has_database=True,
            procedures=holder,
        )
        llm = _LLM()
        tool_bridge.register_tools(llm, [move_to_step_tool(config.procedures)], context)

        # Skipping the confirmation is refused by the procedure itself, not
        # answered by the mock.
        refused = await _move(llm, "done")
        accepted = await _move(llm, "confirm")

        assert json.loads(refused)["success"] is False
        assert json.loads(accepted)["success"] is True
        assert holder.state.run("cancel_order").path == ("identify", "confirm")
        assert mocks.refused == []
        assert [(c["arguments"]["to"], c["outcome"]) for c in mocks.calls] == [
            ("done", "applied"),
            ("confirm", "applied"),
        ]
        assert [c["result"] for c in mocks.calls] == [refused, accepted]


class TestAMockForItIsIgnored:
    def test_the_run_says_so_naming_the_tool(self) -> None:
        [warning] = warn_never_mocked({"move_to_step": {}, "get_order": {}})

        assert warning["code"] == "mock_ignored"
        assert warning["tools"] == ["move_to_step"]
        assert "move_to_step" in warning["message"]
        # It says why for the tool it names, not for another.
        assert "load_skill" not in warning["message"]

    def test_both_never_mocked_tools_in_one_warning(self) -> None:
        [warning] = warn_never_mocked({"move_to_step": {}, "load_skill": {}})

        assert warning["tools"] == ["load_skill", "move_to_step"]
        assert "load_skill" in warning["message"]
        assert "move_to_step" in warning["message"]


LOOKUP = {
    "name": "find_order",
    "start": "call",
    "steps": [
        {
            "id": "lookup",
            "kind": "check",
            "start": True,
            "tool": "get_order",
            "text": "Look the order up.",
            "moves": [{"to": "done", "when": "the order was found"}],
        },
        {"id": "done", "kind": "end", "text": "Read the order out."},
    ],
}


class TestAMockedStepToolSaysWhereToMove:
    async def test_the_model_reads_the_reminder_and_the_record_keeps_the_mock(
        self,
    ) -> None:
        """An eval reads what a call reads: the step tool's result says to
        report the move, mocked or not."""
        mocks = ToolMocks(responses={"get_order": {"status": "shipped"}})
        config = AgentConfig(system_prompt="s", procedures=[LOOKUP])
        context = SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            mcp_manager=None,
            tool_mocks=mocks,
            is_eval=True,
            records=False,
            has_database=True,
            procedures=CallProcedures(ProcedureState.at_call_start(config.procedures)),
        )
        get_order = ToolDefinition(
            name="get_order",
            description="Look an order up.",
            parameters_schema={"type": "object", "properties": {}},
            webhook_url="https://example.com/orders",
        )
        llm = _LLM()
        tool_bridge.register_tools(llm, [get_order], context)
        callback = AsyncMock()

        await llm.handlers["get_order"](
            SimpleNamespace(
                function_name="get_order", arguments={}, result_callback=callback
            )
        )

        read = callback.await_args.args[0]
        assert json.loads(read.split("\n\n")[0]) == {"status": "shipped"}
        assert "report it with move_to_step before you reply: done" in read
        [call] = mocks.calls
        assert "[Procedure]" not in call["result"]


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


class TestASelfDeliveringToolReadsTheReminder:
    """`end_call` and `load_skill` hand back their own result, from inside
    the handler's branch: the reminder rides on the callback itself, on a
    live call as in an eval."""

    @pytest.mark.parametrize("tool", ["end_call", "load_skill"])
    async def test_a_steps_own_tool_gets_it(self, monkeypatch, tool) -> None:
        async def builtin(*_args):
            return '{"success": true, "message": "ok", "details": null}'

        async def load(_args, _context, params):
            await params.result_callback("loaded", properties="kept")
            return "loaded"

        monkeypatch.setattr(tool_bridge, "_execute_builtin", builtin)
        monkeypatch.setattr(skill_loader, "run_load_skill", load)
        config = AgentConfig(system_prompt="s", procedures=[_wrap_up(tool)])
        context = SimpleNamespace(
            call_id=uuid4(),
            project_id=uuid4(),
            mcp_manager=None,
            tool_mocks=None,
            is_eval=False,
            records=False,
            has_database=True,
            procedures=CallProcedures(ProcedureState.at_call_start(config.procedures)),
        )
        llm = _LLM()
        tool_bridge.register_tools(
            llm,
            [ToolDefinition(name=tool, description="d", parameters_schema={})],
            context,
        )
        answer = AsyncMock()
        params = SimpleNamespace(
            function_name=tool,
            tool_call_id="1",
            arguments={},
            llm=SimpleNamespace(push_frame=AsyncMock()),
            context=None,
            result_callback=answer,
        )

        await llm.handlers[tool](params)

        read = answer.await_args.args[0]
        assert "\n\n[Procedure] 'wrap_up' is at step 'wrap', whose tool" in read
        if tool == "load_skill":
            assert answer.await_args.kwargs == {"properties": "kept"}
