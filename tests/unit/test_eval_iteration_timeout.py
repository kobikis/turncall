"""A hung iteration must not hold a worker slot for the life of the process (#93).

`await harness.run()` had no time budget, while every service the harness
itself drives — the persona LLM, its TTS, its STT, the judge — is a network
call. A provider that accepts a connection and then never answers is the
ordinary failure, and it left `execute_run` never returning: the janitor swept
the *row* to `errored` while nothing swept the *task*, so four hangs produced a
worker that consumed no queue, executed nothing, logged nothing, and still
answered as healthy.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from turncall.config.settings import EvalSettings
from turncall.domain.enums import EvalRunStatus
from turncall.domain.models import AgentConfig
from turncall.evals.harness import IterationTimeout, _run_harness, run_iteration
from turncall.evals.runner import (
    _MIN_ITERATION_BUDGET_S,
    ResolvedTarget,
    iteration_budget_seconds,
)

pytestmark = pytest.mark.unit


def _harness(run):
    return SimpleNamespace(run=run)


@pytest.mark.unit
class TestTheBudget:
    """Derived from the run budget and the work asked for, not a new constant:
    one number should govern how long a run may take (#94 reads the same one)."""

    def test_it_is_the_run_budget_split_across_the_iterations(self) -> None:
        settings = SimpleNamespace(evals=EvalSettings(max_run_duration_seconds=900))
        assert iteration_budget_seconds(settings, 2) == 450.0

    def test_a_high_iteration_count_does_not_squeeze_it_to_nothing(self) -> None:
        """900s split 50 ways is 18s, which one audio simulation exceeds by
        design. A budget that tight abandons a run nobody misconfigured."""
        settings = SimpleNamespace(evals=EvalSettings(max_run_duration_seconds=900))
        assert iteration_budget_seconds(settings, 50) == _MIN_ITERATION_BUDGET_S

    def test_settings_that_say_nothing_still_give_a_budget(self) -> None:
        assert iteration_budget_seconds(SimpleNamespace(), 1) == _MIN_ITERATION_BUDGET_S


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheHarnessIsBounded:
    async def test_a_harness_that_never_returns_raises(self) -> None:
        async def never():
            await asyncio.Event().wait()

        with pytest.raises(IterationTimeout) as caught:
            await _run_harness(_harness(never), 0.02, uuid4())
        assert "did not finish within" in str(caught.value)

    async def test_an_iteration_inside_the_budget_is_untouched(self) -> None:
        async def quick():
            await asyncio.sleep(0.01)
            return "the result"

        assert await _run_harness(_harness(quick), 5.0, uuid4()) == "the result"

    async def test_no_budget_means_no_wrapper(self) -> None:
        async def quick():
            return "the result"

        assert await _run_harness(_harness(quick), None, uuid4()) == "the result"


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheIterationGivesEverythingBack:
    async def test_the_bot_pipeline_is_still_torn_down(self) -> None:
        """The timeout must not leak the pipeline it was driving: one left
        running holds provider sockets and its port for the life of the
        worker."""
        bot_started = asyncio.Event()

        async def bot_forever():
            bot_started.set()
            await asyncio.Event().wait()

        session = SimpleNamespace(start=bot_forever, failure=None)

        async def never():
            await asyncio.Event().wait()

        import turncall.evals.harness as harness_mod

        original = harness_mod._BOT_STOP_TIMEOUT_S
        harness_mod._BOT_STOP_TIMEOUT_S = 0.05
        try:
            with (
                patch(
                    "turncall.orchestrator.transport_factory.create_eval_transport",
                    MagicMock(),
                ),
                patch(
                    "turncall.orchestrator.pipeline_builder.build_call_pipeline",
                    AsyncMock(return_value=session),
                ),
                patch.object(
                    harness_mod,
                    "_build_session",
                    MagicMock(return_value=_harness(never)),
                ),
            ):
                with pytest.raises(IterationTimeout):
                    await run_iteration(
                        parsed=MagicMock(),
                        kind=MagicMock(),
                        target=SimpleNamespace(
                            project_id=uuid4(), config=AgentConfig(), agent_id=uuid4()
                        ),
                        modality=MagicMock(),
                        settings=SimpleNamespace(),
                        session_factory=MagicMock(),
                        run_id=uuid4(),
                        tool_mocks=MagicMock(),
                        timeout_s=0.02,
                    )
        finally:
            harness_mod._BOT_STOP_TIMEOUT_S = original

        assert bot_started.is_set(), "the bot never started — test proves nothing"
        assert not [
            t for t in asyncio.all_tasks() if t.get_name().startswith("eval-bot-")
        ], "the pipeline task outlived the iteration"

    async def test_a_second_run_proceeds_after_the_first_one_hangs(self) -> None:
        """The worker's real loop, with one slot: a bounded run gives it back,
        so the queue keeps moving. Unbounded, this is the hang that leaves a
        worker consuming nothing while still answering as healthy."""
        from turncall.evals import worker as worker_mod

        stop = asyncio.Event()
        slots = asyncio.Semaphore(1)
        ran: list[str] = []
        queued = ["hangs", "follows"]

        async def dequeue(_redis, timeout=5):
            if queued:
                return queued.pop(0)
            stop.set()
            return None

        async def execute_run(run_id, **_kw):
            ran.append(run_id)
            if run_id == "hangs":
                # What the bound turns a hang into: a raise, eventually.
                await asyncio.sleep(0.01)
                raise IterationTimeout("the harness did not finish within 180s")

        with (
            patch("turncall.storage.redis.get_redis", MagicMock()),
            patch("turncall.evals.runner.execute_run", execute_run),
            patch.object(worker_mod.eval_queue, "dequeue", dequeue),
        ):
            await asyncio.wait_for(
                worker_mod._consume(MagicMock(), SimpleNamespace(), stop, slots),
                timeout=5,
            )

        assert ran == ["hangs", "follows"], "the slot was never given back"


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheRunStillCompletes:
    """The run reaches a verdict instead of hanging — which is what gives the
    worker its slot back."""

    @staticmethod
    def _run_row(iterations=2):
        return SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            status="queued",
            modality="text",
            kind="script",
            iterations=iterations,
            scenario_name="greets",
            scenario_id=uuid4(),
            batch_id=None,
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={"definition": {"turns": [{"user": "hi", "expect": []}]}},
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
        )

    async def _execute_run(self, run, execute):
        from turncall.evals import runner as runner_mod

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
                settings=SimpleNamespace(evals=EvalSettings()),
                execute=execute,
            )
        return finish

    async def test_a_timed_out_iteration_is_errored_and_says_so(self) -> None:
        run = self._run_row()
        finish = await self._execute_run(
            run, AsyncMock(side_effect=IterationTimeout("did not finish within 180s"))
        )
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED, (
            "a harness that did not complete says nothing about the agent"
        )
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert "did not finish within" in kwargs["results"][0]["error"]

    async def test_the_iteration_is_handed_the_derived_budget(self) -> None:
        """Otherwise the wrapper is dead code: the plan has to carry it."""
        run = self._run_row(iterations=2)
        execute = AsyncMock(side_effect=IterationTimeout("nope"))
        await self._execute_run(run, execute)
        assert execute.await_args.kwargs["timeout_s"] == iteration_budget_seconds(
            SimpleNamespace(evals=EvalSettings()), 2
        )
