"""Cancelling a *running* eval run has to actually stop it (#92).

`DELETE /v1/eval-runs/{id}` set the column and nothing else: every remaining
iteration still ran and was paid for, then the loop's verdict was written
straight over `cancelled` and announced as `eval.run.completed`. Three rules
hold it together now — the loop notices between iterations, `finish_run`
refuses to move a terminal row, and a verdict that was not written is not
announced.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.enums import EvalRunStatus
from turncall.domain.models import AgentConfig
from turncall.evals.runner import ResolvedTarget
from turncall.evals.runner import logger as runner_logger

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _result():
    return SimpleNamespace(
        scenario_name="greets",
        passed=True,
        skipped=None,
        duration_ms=10,
        failures=[],
        turns=[],
        events_seen=[],
        debug_log=[],
    )


def _run_row(iterations: int, status: str = "queued"):
    return SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        status=status,
        modality="text",
        kind="script",
        iterations=iterations,
        scenario_name="greets",
        scenario_id=uuid4(),
        batch_id=uuid4(),
        target={"type": "agent", "agent_id": str(uuid4())},
        passed_count=0,
        failed_count=0,
        error=None,
        results=[],
        agent_id=None,
        agent_version=None,
        resolved_config={},
        harness_config={},
        queued_at=None,
        started_at=None,
        completed_at=None,
        resolved_scenario={
            "definition": {
                "turns": [{"user": "hello", "expect": [{"event": "llm_response"}]}]
            }
        },
    )


def _factory():
    session = AsyncMock()

    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return False

    return (lambda: _CM()), session


async def _execute_run(run_row, *, get_run, finish=None, execute=None):
    """The real `execute_run`, with the repository and the harness faked."""
    from turncall.evals import runner as runner_mod

    factory, _ = _factory()
    finish = finish or AsyncMock(return_value=True)
    execute = execute or AsyncMock(return_value=_result())
    target = ResolvedTarget(
        project_id=run_row.project_id,
        config=AgentConfig(),
        config_blob={"system_prompt": "hi"},
        agent_id=uuid4(),
    )
    dispatched: list[str] = []

    async def _capture(_session, *, event_type, **_kw):
        dispatched.append(event_type.value)

    with (
        patch("turncall.storage.repositories.eval_repo.get_run", get_run),
        patch("turncall.storage.repositories.eval_repo.start_run", AsyncMock()),
        patch("turncall.storage.repositories.eval_repo.finish_run", finish),
        patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
        patch.object(runner_mod, "_dispatch_run_event", _capture),
    ):
        await runner_mod.execute_run(
            run_row.id,
            session_factory=factory,
            settings=SimpleNamespace(),
            execute=execute,
        )
    return execute, finish, dispatched


class TestTheLoopNoticesTheCancellation:
    async def test_it_stops_between_iterations(self) -> None:
        """Cancelling a 50-iteration run has to stop paying for it — that is
        most of the reason to offer cancel at all."""
        run = _run_row(iterations=5)
        cancelled_after = 2

        async def get_run(_session, _run_id, **_kw):
            get_run.calls += 1
            # Call 1 is `execute_run`'s claim check; the rest are the loop's.
            if get_run.calls > cancelled_after + 1:
                return SimpleNamespace(**{**vars(run), "status": "cancelled"})
            return run

        get_run.calls = 0

        execute, finish, _dispatched = await _execute_run(run, get_run=get_run)

        assert execute.await_count == cancelled_after, "the loop ran on after cancel"
        assert finish.await_args.kwargs["status"] is not EvalRunStatus.CANCELLED
        assert len(finish.await_args.kwargs["results"]) == cancelled_after

    async def test_a_database_hiccup_is_not_a_cancellation(self, caplog) -> None:
        """A check that cannot be made must not abandon a run nobody stopped.

        Asserting the run completes is not enough — an unchecked loop does that
        too, so this pins that the check *was* attempted, failed, and said why.
        """
        run = _run_row(iterations=3)
        calls = {"n": 0}

        async def get_run(_session, _run_id, **_kw):
            calls["n"] += 1
            # Call 1 is the claim check, the next three are the loop's, and the
            # last is `_finish_run` reading the row back to announce it.
            if 1 < calls["n"] <= 4:
                raise RuntimeError("connection reset")
            return run

        logged: list[str] = []
        with patch.object(
            runner_logger, "warning", lambda event, **kw: logged.append((event, kw))
        ):
            execute, _finish, _dispatched = await _execute_run(run, get_run=get_run)

        assert execute.await_count == 3, "a failed check stopped a healthy run"
        failures = [kw for event, kw in logged if event == "eval_cancel_check_failed"]
        assert len(failures) == 3, "the check was never attempted"
        assert "connection reset" in failures[0]["error"], (
            "a bare log line does not say which check is failing, or why"
        )


class TestTheVerdictDoesNotOverwriteIt:
    async def test_a_declined_write_is_not_announced(self) -> None:
        """`finish_run` reporting 0 rows means the row went terminal without
        this loop. No `eval.run.completed` for a run the user cancelled."""
        run = _run_row(iterations=1)
        _execute, _finish, dispatched = await _execute_run(
            run,
            get_run=AsyncMock(return_value=run),
            finish=AsyncMock(return_value=False),
        )
        assert dispatched == ["eval.run.started"]

    async def test_a_landed_write_still_announces(self) -> None:
        run = _run_row(iterations=1)
        _execute, _finish, dispatched = await _execute_run(
            run,
            get_run=AsyncMock(return_value=run),
            finish=AsyncMock(return_value=True),
        )
        assert dispatched == ["eval.run.started", "eval.run.completed"]

    async def test_an_unrunnable_run_cancelled_first_is_left_alone(self) -> None:
        """Cancelled between the claim and the failure: the row is already
        terminal and this path does not get to relabel it."""
        from turncall.evals import runner as runner_mod

        run = _run_row(iterations=1)
        _factory_fn, session = _factory()
        dispatched: list[str] = []

        async def _capture(_session, *, event_type, **_kw):
            dispatched.append(event_type.value)

        with (
            patch(
                "turncall.storage.repositories.eval_repo.finish_run",
                AsyncMock(return_value=False),
            ),
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run),
            ),
            patch.object(runner_mod, "_dispatch_run_event", _capture),
        ):
            await runner_mod._record_unrunnable(session, run, ValueError("no agent"))

        assert dispatched == []


class TestAQueuedCancellationIsUnchanged:
    async def test_a_cancelled_queued_run_is_never_claimed(self) -> None:
        run = _run_row(iterations=1, status="cancelled")
        execute, finish, dispatched = await _execute_run(
            run, get_run=AsyncMock(return_value=run)
        )
        assert execute.await_count == 0
        assert finish.await_count == 0
        assert dispatched == []
