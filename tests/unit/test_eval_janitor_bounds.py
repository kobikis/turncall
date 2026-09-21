"""The janitor's cutoff is derived from the work the run was asked to do (#94).

`started_at` is stamped once in `start_run` and never renewed — there is no
heartbeat and no lease — so "older than 900 seconds" was the only evidence used
to conclude a worker had died, and a healthy 10-iteration run looked exactly
like a crashed one. It was swept to `errored` mid-flight; the CLI takes the
first terminal status it sees, so `turncall eval run` exited 2 on a run that
passed. Now that every iteration is bounded (#93), the run's ceiling is
knowable and the cutoff is that, per row.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.config.settings import EvalSettings
from turncall.evals.runner import (
    MIN_ITERATION_BUDGET_S,
    RECLAIM_MARGIN_S,
    iteration_budget_seconds,
    run_budget_seconds,
)

pytestmark = pytest.mark.unit

SETTINGS = SimpleNamespace(evals=EvalSettings(max_run_duration_seconds=900))


class TestTheRunBudget:
    def test_one_iteration_keeps_the_whole_run_budget(self) -> None:
        assert run_budget_seconds(SETTINGS, 1) == 900.0 + RECLAIM_MARGIN_S

    def test_ten_iterations_get_ten_iterations_worth(self) -> None:
        """The failing case from the issue: a 10-iteration audio simulation at
        a minute or two per conversation passes 900s comfortably."""
        assert run_budget_seconds(SETTINGS, 10) == 10 * 180.0 + RECLAIM_MARGIN_S
        assert run_budget_seconds(SETTINGS, 10) > 900.0

    def test_the_sql_cutoff_is_the_same_arithmetic(self) -> None:
        """The janitor cannot call `run_budget_seconds` per row, so it rebuilds
        it as `greatest(max_age, min_iteration * iterations) + margin`. This
        pins the two together — the SQL is only correct while this holds."""
        whole = float(SETTINGS.evals.max_run_duration_seconds)
        for n in range(1, EvalSettings().max_iterations + 1):
            in_sql = max(whole, MIN_ITERATION_BUDGET_S * n) + RECLAIM_MARGIN_S
            assert run_budget_seconds(SETTINGS, n) == pytest.approx(in_sql), n

    def test_it_is_never_shorter_than_the_iterations_it_allows(self) -> None:
        """A run swept while an iteration it was granted is still inside its
        own budget is the bug, restated."""
        for n in (1, 3, 10, 50):
            assert (
                run_budget_seconds(SETTINGS, n)
                > n * iteration_budget_seconds(SETTINGS, n) - 1e-9
            )


@pytest.mark.asyncio
class TestTheJanitorPassesThem:
    async def test_the_sweep_is_given_the_derived_bounds(self) -> None:
        """Otherwise the repository quietly falls back to the flat cutoff."""
        from turncall.evals import worker as worker_mod

        stop = asyncio.Event()
        sweep = AsyncMock(return_value=0)

        async def _sweep(session, **kwargs):
            stop.set()
            return await sweep(session, **kwargs)

        class _CM:
            async def __aenter__(self):
                return AsyncMock()

            async def __aexit__(self, *_):
                return False

        settings = SimpleNamespace(
            evals=EvalSettings(janitor_interval_seconds=0, max_run_duration_seconds=900)
        )
        with patch(
            "turncall.storage.repositories.eval_repo.reclaim_stalled_runs", _sweep
        ):
            await asyncio.wait_for(
                worker_mod._janitor(
                    MagicMock(side_effect=lambda: _CM()), settings, stop
                ),
                timeout=5,
            )

        kwargs = sweep.await_args.kwargs
        assert kwargs["min_iteration_seconds"] == MIN_ITERATION_BUDGET_S
        assert kwargs["margin_seconds"] == RECLAIM_MARGIN_S
        assert kwargs["max_age_seconds"] == 900
