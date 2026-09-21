"""A pipeline that dies is `errored`, never `failed`.

This is the core rule of the design applied to the one case that silently broke
it. When the agent's pipeline stops — an unusable LLM, a bad key, a provider
400 — the harness sits there matching nothing and times out, and a timeout
reads as the agent falling short. It is not: nobody learned anything about the
agent, so the run must be `errored` and count toward neither rate.

The reason it was silent is that `CallSession.start()` swallows its own
exception on purpose (a live call has to finalize rather than propagate), so
the bot task completes cleanly and there is nothing for the eval side to catch.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from turncall.evals.harness import PipelineFailed, _stop_bot

pytestmark = pytest.mark.unit


def _session(call_id):
    from turncall.orchestrator.call_session import CallSession

    context = MagicMock()
    context.call_id = call_id
    context.is_eval = True
    return CallSession(
        call_context=context, transport=MagicMock(), pipeline=MagicMock()
    )


def test_a_fresh_session_reports_no_failure() -> None:
    assert _session(uuid4()).failure is None


def test_call_session_keeps_the_exception_it_swallows() -> None:
    """start() logs and moves on so a live call still finalizes. The eval side
    needs the exception itself, which is why it is kept rather than only
    logged."""
    session = _session(uuid4())
    boom = RuntimeError("LLM service is unusable")
    # What start()'s `except Exception` branch does.
    session._failure = boom
    assert session.failure is boom


@pytest.mark.asyncio
async def test_stop_bot_tolerates_a_task_that_already_finished() -> None:
    async def done() -> None:
        return None

    task = asyncio.create_task(done())
    await asyncio.sleep(0)
    await _stop_bot(task, uuid4())


@pytest.mark.asyncio
async def test_stop_bot_cancels_a_pipeline_that_will_not_wind_down() -> None:
    """A leaked pipeline holds provider sockets and its port for the life of
    the worker."""
    import turncall.evals.harness as harness_mod

    async def forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(forever())
    original = harness_mod._BOT_STOP_TIMEOUT_S
    harness_mod._BOT_STOP_TIMEOUT_S = 0.05
    try:
        await _stop_bot(task, uuid4())
    finally:
        harness_mod._BOT_STOP_TIMEOUT_S = original
    assert task.cancelled() or task.done()


class TestTheRunnerScoresItAsErrored:
    """`run_iteration` raises PipelineFailed; the runner turns any raise into an
    errored iteration, which is what keeps it out of both rates."""

    async def test_a_pipeline_failure_errors_the_run(self) -> None:
        from unittest.mock import AsyncMock, patch

        from turncall.domain.enums import EvalRunStatus
        from turncall.domain.models import AgentConfig
        from turncall.evals import runner as runner_mod
        from turncall.evals.runner import ResolvedTarget

        run = SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            status="queued",
            modality="text",
            kind="script",
            iterations=2,
            scenario_name="greets",
            scenario_id=uuid4(),
            batch_id=None,
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={"definition": {"turns": [{"user": "hi", "expect": []}]}},
            # The fields a finished row carries: `eval.run.completed` is built
            # by reading the row back (#76).
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

        class _CM:
            async def __aenter__(self):
                return AsyncMock()

            async def __aexit__(self, *_):
                return False

        finish = AsyncMock()
        target = ResolvedTarget(
            project_id=run.project_id,
            config=AgentConfig(),
            config_blob={},
            agent_id=uuid4(),
        )
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run),
            ),
            patch("turncall.storage.repositories.eval_repo.start_run", AsyncMock()),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
        ):
            await runner_mod.execute_run(
                run.id,
                session_factory=lambda: _CM(),
                settings=SimpleNamespace(),
                execute=AsyncMock(
                    side_effect=PipelineFailed("the agent pipeline stopped")
                ),
            )

        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED, (
            "a dead pipeline is not the agent falling short"
        )
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert "pipeline stopped" in kwargs["results"][0]["error"]
