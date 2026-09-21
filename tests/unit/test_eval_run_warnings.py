"""A mock that can never fire has to reach the person who wrote it (#96).

`_warn_unmatched_mocks` diagnosed this correctly and then wrote the answer to
the `turncall-eval-worker` container's log. The author reads the run — the API,
the Console, the CLI — so from where they stand a mock keyed to an MCP tool (an
eval advertises none), a typo'd tool name, and a tool the agent simply chose not
to call all looked identical: a green run with an inert mock.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig, MCPServerConfig, ToolDefinition
from turncall.evals.runner import ResolvedTarget, _warn_unmatched_mocks

pytestmark = pytest.mark.unit


def _target(*, tools=(), mcp_servers=()):
    return ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig(
            tools=[
                ToolDefinition(name=n, description=n, parameters_schema={})
                for n in tools
            ],
            mcp_servers=[
                MCPServerConfig(name=n, transport="http", url="https://x/mcp")
                for n in mcp_servers
            ],
        ),
        config_blob={},
        agent_id=uuid4(),
    )


class TestTheWarningIsReturnedNotJustLogged:
    def test_a_mock_naming_no_tool_is_reported(self) -> None:
        warnings = _warn_unmatched_mocks(
            {"book_table": {"ok": True}}, _target(tools=["check_hours"]), run_id=uuid4()
        )
        assert len(warnings) == 1
        assert warnings[0]["code"] == "mock_matches_no_tool"
        assert warnings[0]["tools"] == ["book_table"]
        assert "book_table" in warnings[0]["message"]

    def test_an_mcp_agent_is_told_it_is_the_mcp_limitation(self) -> None:
        """The server count is the valuable half: it separates "evals never
        advertise MCP tools" from "you typo'd the name"."""
        warnings = _warn_unmatched_mocks(
            {"crm_lookup": {}}, _target(mcp_servers=["crm"]), run_id=uuid4()
        )
        assert warnings[0]["mcp_servers"] == 1
        assert "MCP" in warnings[0]["message"]

    def test_an_agent_without_mcp_is_told_to_check_the_name(self) -> None:
        warnings = _warn_unmatched_mocks({"typo": {}}, _target(), run_id=uuid4())
        assert warnings[0]["mcp_servers"] == 0
        assert "tool name" in warnings[0]["message"]

    def test_mocks_that_all_match_warn_about_nothing(self) -> None:
        assert (
            _warn_unmatched_mocks(
                {"check_hours": {}}, _target(tools=["check_hours"]), run_id=uuid4()
            )
            == []
        )

    def test_a_builtin_is_a_tool_the_run_can_call(self) -> None:
        assert _warn_unmatched_mocks({"end_call": {}}, _target(), run_id=uuid4()) == []

    def test_no_mocks_at_all_warns_about_nothing(self) -> None:
        assert _warn_unmatched_mocks({}, _target(tools=["a"]), run_id=uuid4()) == []


@pytest.mark.asyncio
class TestTheyReachTheRunAndTheEvent:
    @staticmethod
    def _run_row(**over):
        base = dict(
            id=uuid4(),
            project_id=uuid4(),
            status="queued",
            modality="text",
            kind="script",
            iterations=1,
            scenario_name="greets",
            scenario_id=uuid4(),
            batch_id=None,
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={
                "definition": {"turns": [{"user": "hi", "expect": []}]},
                "tool_mocks": {"book_table": {"response": {"ok": True}}},
                "tool_policy": "mock_only",
            },
            passed_count=0,
            failed_count=0,
            error=None,
            results=[],
            warnings=[],
            agent_id=None,
            agent_version=None,
            resolved_config={},
            harness_config={},
            queued_at=None,
            started_at=None,
            completed_at=None,
        )
        base.update(over)
        return SimpleNamespace(**base)

    async def test_the_run_row_is_written_with_them(self) -> None:
        from turncall.evals import runner as runner_mod

        run = self._run_row()
        start = AsyncMock()
        with (
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch.object(
                runner_mod,
                "resolve_target",
                AsyncMock(return_value=_target(mcp_servers=["crm"])),
            ),
            patch.object(runner_mod, "_dispatch_run_event", AsyncMock()),
        ):
            await runner_mod._plan_run(
                AsyncMock(),
                run,
                settings=SimpleNamespace(),
                session_factory=MagicMock(),
            )

        written = start.await_args.kwargs["warnings"]
        # Alongside whatever else the run has to say about itself (#95).
        assert "mock_matches_no_tool" in [w["code"] for w in written]

    async def test_the_completed_event_carries_them(self) -> None:
        """It is built off the row, so this comes free — but free is not the
        same as tested, and a subscriber is the other surface that needs it."""
        from turncall.evals import runner as runner_mod

        warning = {"code": "mock_matches_no_tool", "tools": ["book_table"]}
        payload = runner_mod._completed_payload(
            self._run_row(status="passed", warnings=[warning])
        )
        assert payload["warnings"] == [warning]

    async def test_a_clean_run_carries_an_empty_list(self) -> None:
        from turncall.evals import runner as runner_mod

        payload = runner_mod._completed_payload(self._run_row(status="passed"))
        assert payload["warnings"] == []


class TestTheCliPrintsThem:
    def test_a_warning_prints_with_the_run(self, capsys) -> None:
        from turncall.cli import main as cli

        cli._print_warnings(
            {"warnings": [{"code": "x", "message": "a mock that cannot fire"}]}
        )
        assert "warning: a mock that cannot fire" in capsys.readouterr().out

    def test_a_clean_run_prints_nothing(self, capsys) -> None:
        from turncall.cli import main as cli

        cli._print_warnings({"warnings": []})
        cli._print_warnings({})
        assert capsys.readouterr().out == ""
