"""The eval runner's orchestration, with canned pipecat results.

No pipeline and no socket: everything that needs those lives behind the injected
`execute`, so what is covered here is the tool policy, the three snapshots, the
iteration loop and the status the row ends on. Mapping a single result is
`test_eval_result_mapping.py`; resolving the target is `test_eval_targets.py`.

The rule under test throughout is that `errored` is not a kind of `failed`. An
iteration whose harness did not complete is neither a pass nor a fail and never
counts toward a rate.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tests.unit.eval_results import (
    failure,
    script_result,
    simulation_result,
    turn_result,
)
from turncall.domain.enums import EvalKind, EvalRunStatus
from turncall.domain.models import AgentConfig
from turncall.evals.runner import tool_policy_of
from turncall.evals.snapshots import harness_config, resolved_scenario_snapshot
from turncall.evals.targets import ResolvedTarget, TargetError

pytestmark = pytest.mark.unit


class TestToolPolicyResolution:
    """One place decides the fail-closed default — deciding it twice is how the
    safe value stops being the default (#71)."""

    def test_live_has_to_be_named_exactly(self) -> None:
        from turncall.domain.enums import EvalToolPolicy

        assert EvalToolPolicy.resolve("live") is EvalToolPolicy.LIVE
        assert EvalToolPolicy.resolve(EvalToolPolicy.LIVE) is EvalToolPolicy.LIVE

    def test_anything_else_fails_closed(self) -> None:
        """Including a value a future version wrote: a stored row must never
        make this raise inside a worker, and must never become `live` by
        accident."""
        from turncall.domain.enums import EvalToolPolicy

        for value in (None, "", "mock_only", "LIVE", "whatever", 7, {"a": 1}):
            assert EvalToolPolicy.resolve(value) is EvalToolPolicy.MOCK_ONLY

    def test_the_run_reads_its_mocks_off_the_snapshot(self) -> None:
        """The scenario row can be edited while the run sits queued; a run is
        what it was queued as."""
        from turncall.evals.runner import tool_policy_of

        mocks, live = tool_policy_of(
            {"tool_mocks": {"book": {"ok": True}}, "tool_policy": "live"}
        )
        assert (mocks, live) == ({"book": {"ok": True}}, True)

    def test_a_snapshot_missing_both_is_mock_only_with_no_mocks(self) -> None:
        assert tool_policy_of({}) == ({}, False)


class TestSnapshots:
    def test_the_scenario_snapshot_carries_the_mocks_and_the_policy(self) -> None:
        """Mocks are part of what the test means, so a run that does not record
        them cannot be compared with the next one."""
        snapshot = resolved_scenario_snapshot(
            definition={"turns": []},
            schema_version="pipecat-1.11",
            tool_mocks={"book": {"ok": True}},
            tool_policy="mock_only",
        )
        assert snapshot == {
            "definition": {"turns": []},
            "schema_version": "pipecat-1.11",
            "tool_mocks": {"book": {"ok": True}},
            "tool_policy": "mock_only",
            "tool_policy_enforced": True,
            # Which LLM decided the verdict is part of the same record (#118);
            # null is a scenario that left pipecat's default alone.
            "judge": None,
            "simulator": None,
        }

    def test_the_snapshot_says_whether_the_policy_was_enforced(self) -> None:
        """Runs written before #71 say False: their `mock_only` was a column
        default nothing honoured, and a reader comparing across the slice has
        to be able to tell the two apart."""
        snapshot = resolved_scenario_snapshot(
            definition={},
            schema_version="pipecat-1.11",
            tool_mocks={},
            tool_policy="mock_only",
        )
        assert snapshot["tool_policy_enforced"] is True

    def test_the_harness_snapshot_names_the_judge_and_pipecat(self) -> None:
        """The judge decides the verdict; a silent provider-side model update
        moves the whole baseline, so the run has to say which one answered."""
        parsed = SimpleNamespace(
            judge={"service": "openai", "model": "gpt-4o-2024-11-20"}
        )
        config = harness_config(parsed)
        assert config["judge_model"] == "gpt-4o-2024-11-20"
        assert config["judge_service"] == "openai"
        assert config["judge_used"] is False, "no eval: assertion asks it anything"
        assert config["pipecat_version"].startswith("1.")
        assert config["schema_version"]

    def test_the_judge_snapshot_records_pipecats_default_too(self) -> None:
        """Pipecat fills `judge.eval:` with ollama/gemma when a scenario names
        nothing — which is not the judge most people assume they are running."""
        from turncall.domain.enums import EvalModality
        from turncall.evals.scenario import parse, with_modality

        parsed = parse(
            with_modality({"turns": [{"user": "hi", "expect": []}]}, EvalModality.TEXT),
            name="defaulted",
        )
        config = harness_config(parsed)
        assert config["judge_service"], "the run must name whatever judge ran"
        assert config["judge_model"]


class TestExecuteRun:
    """The loop, end to end, against a fake repository and a fake harness."""

    @staticmethod
    def _run_row(iterations=1, status="queued"):
        return SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            status=status,
            modality="text",
            kind="script",
            iterations=iterations,
            scenario_name="greets-the-caller",
            scenario_id=uuid4(),
            batch_id=uuid4(),
            target={"type": "agent", "agent_id": str(uuid4())},
            # What a finished row carries. `eval.run.completed` is built by
            # reading the row back (#76), so a fake missing these would pass a
            # test the real path cannot.
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
            resolved_scenario={
                "definition": {
                    "turns": [
                        {
                            "user": "hello",
                            "expect": [
                                {"event": "llm_response", "text_contains": "hi"}
                            ],
                        }
                    ]
                }
            },
        )

    @staticmethod
    def _session_factory():
        session = AsyncMock()

        class _CM:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_):
                return False

        return (lambda: _CM()), session

    async def _execute(self, run_row, execute, *, finish=None, start=None):
        from turncall.evals import runner as runner_mod

        factory, _session = self._session_factory()
        finish = finish or AsyncMock()
        start = start or AsyncMock()
        target = ResolvedTarget(
            project_id=run_row.project_id,
            config=AgentConfig(),
            config_blob={"system_prompt": "hi"},
            agent_id=uuid4(),
        )
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run_row),
            ),
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
            patch.object(
                runner_mod,
                "for_project",
                AsyncMock(return_value=runner_mod.Credentials()),
            ),
        ):
            await runner_mod.execute_run(
                run_row.id,
                session_factory=factory,
                settings=SimpleNamespace(),
                execute=execute,
            )
        return finish, start

    async def test_a_passing_run_records_the_counts_and_the_snapshots(self) -> None:
        execute = AsyncMock(return_value=script_result(turns=[turn_result()]))
        run = self._run_row(iterations=3)
        finish, start = await self._execute(run, execute)

        assert execute.await_count == 3, "one execution per iteration"
        assert start.await_args.kwargs["resolved_config"] == {"system_prompt": "hi"}
        assert start.await_args.kwargs["harness_config"]["pipecat_version"]
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (3, 0)
        assert len(kwargs["results"]) == 3

    async def test_one_failing_iteration_fails_the_run(self) -> None:
        results = [
            script_result(turns=[turn_result()]),
            script_result(
                passed=False,
                failures=[failure()],
                turns=[turn_result(status="failed", failures=[failure()])],
            ),
        ]
        execute = AsyncMock(side_effect=results)
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.FAILED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (1, 1)

    async def test_a_harness_that_raises_errors_rather_than_fails(self) -> None:
        execute = AsyncMock(side_effect=ConnectionRefusedError("nothing listening"))
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert "nothing listening" in kwargs["error"]

    async def test_the_scenarios_mocks_and_policy_reach_the_iteration(self) -> None:
        """#71: the mocks are the scenario's, so every iteration gets the same
        ones — but a fresh recorder, since what was called belongs to the
        iteration."""
        seen = []

        async def execute(**kwargs):
            seen.append(kwargs["tool_mocks"])
            return script_result(turns=[turn_result()])

        run = self._run_row(iterations=2)
        run.resolved_scenario["tool_mocks"] = {"book": {"ok": True}}
        run.resolved_scenario["tool_policy"] = "mock_only"
        await self._execute(run, execute)

        assert [m.responses for m in seen] == [{"book": {"ok": True}}] * 2
        assert [m.live for m in seen] == [False, False]
        assert seen[0] is not seen[1], "a shared recorder would pool the calls"

    async def test_an_unmocked_tool_errors_the_run_naming_it(self) -> None:
        """Fail closed. `errored`, never `failed`: nothing ran, so it says
        nothing about the agent and must stay out of every rate."""

        async def execute(**kwargs):
            kwargs["tool_mocks"].refused.append("book_appointment")
            kwargs["tool_mocks"].record(
                "book_appointment", {}, '{"error": "..."}', outcome="refused"
            )
            return script_result(turns=[turn_result()])

        finish, _ = await self._execute(self._run_row(), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert kwargs["error"] == "unmocked tool: book_appointment"
        entry = kwargs["results"][0]
        assert entry["tool_calls"][0]["tool_name"] == "book_appointment"

    async def test_a_mock_that_can_never_fire_is_called_out(self) -> None:
        """An eval connects no MCP servers, so an MCP tool is never advertised
        and a mock keyed to one means nothing — as does a typo. Silence is the
        worst outcome: the mock reads as a tool that was simply never called."""
        from turncall.evals import runner as runner_mod

        run = self._run_row()
        run.resolved_scenario["tool_mocks"] = {"searchCrm": {"hits": []}}
        with patch.object(runner_mod.logger, "warning") as warn:
            await self._execute(
                run, AsyncMock(return_value=script_result(turns=[turn_result()]))
            )
        assert any(
            call.args and call.args[0] == "eval_mock_matches_no_tool"
            for call in warn.call_args_list
        ), "a mock naming nothing the agent has must be reported"

    async def test_a_refusal_condemns_the_run_even_if_others_passed(self) -> None:
        """The run, not the iteration. A forgotten mock on a ten-iteration run
        used to report PASSED because the other nine never reached the tool —
        precisely the accident the policy exists to prevent. The run also stops
        there: the rest would hit the same missing mock."""
        calls = {"n": 0}

        async def execute(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                kwargs["tool_mocks"].refused.append("book_appointment")
            return script_result(turns=[turn_result()])

        finish, _ = await self._execute(self._run_row(iterations=5), execute)
        kwargs = finish.await_args.kwargs
        assert calls["n"] == 2, "the remaining iterations are not paid for"
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert kwargs["error"] == "unmocked tool: book_appointment"
        # What did happen is still readable per iteration.
        assert kwargs["results"][0]["passed"] is True

    async def test_a_runs_results_carry_what_the_tools_did(self) -> None:
        """An eval has no `calls` row, so the run entry is the only tool record
        there is — and a mocked call has to be distinguishable from a real one."""

        async def execute(**kwargs):
            kwargs["tool_mocks"].record("book", {}, '{"ok": true}', outcome="mocked")
            return script_result(turns=[turn_result()])

        finish, _ = await self._execute(self._run_row(), execute)
        entry = finish.await_args.kwargs["results"][0]
        assert entry["tool_calls"] == [
            {
                "tool_name": "book",
                "arguments": {},
                "result": '{"ok": true}',
                "outcome": "mocked",
            }
        ]

    async def test_one_bad_iteration_does_not_stop_the_rest(self) -> None:
        execute = AsyncMock(
            side_effect=[
                TimeoutError("judge timed out"),
                script_result(turns=[turn_result()]),
            ]
        )
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (1, 0)
        assert kwargs["results"][0]["error"].startswith("TimeoutError")

    async def test_an_unresolvable_target_errors_before_any_iteration(self) -> None:
        from turncall.evals import runner as runner_mod

        factory, _ = self._session_factory()
        run = self._run_row()
        execute = AsyncMock()
        finish = AsyncMock()
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run),
            ),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(
                runner_mod,
                "resolve_target",
                AsyncMock(side_effect=TargetError("agent not found")),
            ),
        ):
            await runner_mod.execute_run(
                run.id,
                session_factory=factory,
                settings=SimpleNamespace(),
                execute=execute,
            )
        execute.assert_not_awaited()
        assert finish.await_args.kwargs["status"] is EvalRunStatus.ERRORED
        assert finish.await_args.kwargs["error"] == "agent not found"

    async def test_unreadable_project_credentials_error_the_run(self) -> None:
        """ADR-0020 fails closed: a run whose project keys cannot be decrypted
        is errored, never judged on the platform's account instead."""
        from turncall.evals import runner as runner_mod
        from turncall.storage.repositories import credential_repo

        factory, _ = self._session_factory()
        run = self._run_row()
        execute = AsyncMock()
        start = AsyncMock()
        finish = AsyncMock()
        target = ResolvedTarget(
            project_id=run.project_id, config=AgentConfig(), config_blob={}
        )
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run),
            ),
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
            patch.object(
                credential_repo,
                "load_documents",
                AsyncMock(side_effect=credential_repo.CredentialKeyError("rotated")),
            ),
        ):
            await runner_mod.execute_run(
                run.id,
                session_factory=factory,
                settings=SimpleNamespace(),
                execute=execute,
            )
        execute.assert_not_awaited()
        start.assert_not_awaited()
        assert finish.await_args.kwargs["status"] is EvalRunStatus.ERRORED
        assert finish.await_args.kwargs["error"] == "rotated"

    async def test_both_run_events_are_dispatched(self) -> None:
        """#76: one event when the run starts, one comprehensive event when it
        finishes. A subscriber should never have to poll."""
        from turncall.domain.enums import CallEventType
        from turncall.evals import runner as runner_mod

        sent: list = []

        async def capture(session, **kwargs):
            sent.append(kwargs)

        execute = AsyncMock(return_value=script_result(turns=[turn_result()]))
        with patch.object(runner_mod, "_dispatch_run_event", capture):
            await self._execute(self._run_row(), execute)

        assert [e["event_type"] for e in sent] == [
            CallEventType.EVAL_RUN_STARTED,
            CallEventType.EVAL_RUN_COMPLETED,
        ]
        started = sent[0]["payload"]
        assert started["scenario_name"] == "greets-the-caller"
        assert started["iterations"] == 1
        # The run id goes in the envelope, never the payload (ADR-0007).
        assert "eval_run_id" not in started
        assert sent[0]["run_id"] == sent[1]["run_id"]

    async def test_a_run_that_cannot_start_still_announces_itself(self) -> None:
        """Accepted and then silent is worse than failed: a subscriber waiting
        for a terminal event would wait forever."""
        from turncall.domain.enums import CallEventType
        from turncall.evals import runner as runner_mod

        sent: list = []

        async def capture(session, **kwargs):
            sent.append(kwargs)

        run = self._run_row()
        run.resolved_scenario = {"definition": {"turns": "nope"}}
        with patch.object(runner_mod, "_dispatch_run_event", capture):
            await self._execute(run, AsyncMock())

        assert [e["event_type"] for e in sent] == [CallEventType.EVAL_RUN_COMPLETED]
        assert sent[0]["agent_id"] is None, "nothing resolved, so no agent to name"

    async def test_a_simulation_runs_through_its_own_mapper(self) -> None:
        """#73. The definition decides which mapper reads the result: handing a
        simulation result to the scripted mapper would report nonsense
        confidently, which is why the kind is read from what parses rather than
        from the column copied at queue time."""
        run = self._run_row()
        run.kind = "simulation"
        run.resolved_scenario = {
            "definition": {
                "persona": "a caller who lost their booking",
                "goal": "recover it",
                "success": "the agent finds it",
            }
        }
        execute = AsyncMock(return_value=simulation_result())
        finish, _ = await self._execute(run, execute)
        kwargs = finish.await_args.kwargs
        assert execute.await_args.kwargs["kind"] is EvalKind.SIMULATION
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert kwargs["results"][0]["goal"]["succeeded"] is True

    async def test_an_unparseable_stored_definition_errors_before_paying(
        self,
    ) -> None:
        """A definition that passed validation once can be made invalid by an
        update; the worker must not discover that per iteration."""
        run = self._run_row(iterations=5)
        run.resolved_scenario = {"definition": {"turns": "nope"}}
        execute = AsyncMock()
        finish = AsyncMock()
        await self._execute(run, execute, finish=finish)
        execute.assert_not_awaited()
        assert finish.await_args.kwargs["status"] is EvalRunStatus.ERRORED

    async def test_the_judge_that_ran_is_recorded_on_the_run(self) -> None:
        """A verdict is not comparable across time without it."""
        execute = AsyncMock(return_value=script_result(turns=[turn_result()]))
        _finish, start = await self._execute(self._run_row(), execute)
        harness = start.await_args.kwargs["harness_config"]
        assert harness["judge_model"], "judge_model must never be null"
        assert harness["judge_service"]

    async def test_a_run_that_is_no_longer_queued_is_left_alone(self) -> None:
        """Cancelled while queued, or already claimed by another worker."""
        execute = AsyncMock()
        finish = AsyncMock()
        await self._execute(self._run_row(status="cancelled"), execute, finish=finish)
        execute.assert_not_awaited()
        finish.assert_not_awaited()


class TestDescribeFailure:
    """A worker older than its source mixes old and new modules; the ImportError
    that produces names a symbol that is plainly in the file."""

    def test_a_stale_worker_says_to_restart(self) -> None:
        from turncall.evals import runner as runner_mod

        exc = ImportError("cannot import name 'X' from 'turncall.services.credentials'")
        with patch.object(
            runner_mod, "worker_build", return_value={"worker_stale": True}
        ):
            text = runner_mod.describe_failure(exc)
        assert text.startswith("ImportError: cannot import name 'X'")
        assert "Restart the eval worker" in text

    def test_a_fresh_worker_reports_the_error_as_is(self) -> None:
        from turncall.evals import runner as runner_mod

        with patch.object(
            runner_mod, "worker_build", return_value={"worker_stale": False}
        ):
            assert runner_mod.describe_failure(ImportError("x")) == "ImportError: x"
            assert (
                runner_mod.describe_failure(ValueError("y"), prefix_type=False) == "y"
            )
