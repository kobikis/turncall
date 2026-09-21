"""A run has a light read, and the lists page (#99).

`GET /v1/eval-runs/{id}` answers "is it done yet" far more often than it
answers "what happened", and the full read carries every iteration's transcript
plus all three snapshots — which the CLI and the Console poll on a timer. #75
built the batch endpoint on exactly that reasoning; this gives a single run the
same option.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.api.errors import BadRequestError
from turncall.api.v1.evals import (
    create_eval_run,
    get_eval_run,
    list_eval_runs,
    list_eval_scenarios,
)
from turncall.api.v1.schemas.evals import EvalRunSummaryResponse

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

AUTH = SimpleNamespace(project_id=uuid4())


def _summary_row(**over):
    base = {
        "id": uuid4(),
        "project_id": AUTH.project_id,
        "batch_id": None,
        "scenario_id": uuid4(),
        "scenario_name": "greets",
        "kind": "script",
        "modality": "text",
        "status": "passed",
        "iterations": 3,
        "passed_count": 3,
        "failed_count": 0,
        "agent_id": None,
        "agent_version": None,
        "error": None,
        "queued_at": datetime.now(UTC),
        "started_at": None,
        "completed_at": None,
    }
    base.update(over)
    return base


class TestTheLightRead:
    async def test_a_summary_view_never_touches_the_heavy_read(self) -> None:
        summary = AsyncMock(return_value=_summary_row())
        full = AsyncMock()
        with (
            patch("turncall.storage.repositories.eval_repo.get_run_summary", summary),
            patch("turncall.storage.repositories.eval_repo.get_run", full),
        ):
            result = await get_eval_run(uuid4(), AUTH, AsyncMock(), view="summary")

        assert full.await_count == 0, "the point is not reading the transcripts"
        data = result["data"]
        assert data.status == "passed" and data.passed_count == 3
        assert not hasattr(data, "results")
        assert not hasattr(data, "resolved_config")
        assert not hasattr(data, "harness_config")

    async def test_the_summary_carries_what_a_poller_asks_for(self) -> None:
        fields = set(EvalRunSummaryResponse.model_fields)
        assert {"status", "passed_count", "failed_count", "iterations", "error"} <= (
            fields
        )
        assert not fields & {
            "results",
            "resolved_config",
            "resolved_scenario",
            "harness_config",
            "target",
        }

    async def test_the_full_read_is_still_the_default(self) -> None:
        full = AsyncMock(return_value=None)
        with patch("turncall.storage.repositories.eval_repo.get_run", full):
            with pytest.raises(Exception):
                await get_eval_run(uuid4(), AUTH, AsyncMock())
        assert full.await_count == 1

    async def test_an_unknown_view_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(BadRequestError, match="summary"):
            await get_eval_run(uuid4(), AUTH, AsyncMock(), view="brief")


class TestTheListsPage:
    async def test_runs_are_paged_and_counted(self) -> None:
        rows = AsyncMock(return_value=[])
        count = AsyncMock(return_value=137)
        with (
            patch("turncall.storage.repositories.eval_repo.list_runs", rows),
            patch("turncall.storage.repositories.eval_repo.count_runs", count),
        ):
            result = await list_eval_runs(AUTH, AsyncMock(), page=3, limit=20)

        assert rows.await_args.kwargs["limit"] == 20
        assert rows.await_args.kwargs["offset"] == 40, "page 3 of 20"
        assert (result["total"], result["page"], result["limit"]) == (137, 3, 20)

    async def test_scenarios_are_paged_and_counted(self) -> None:
        rows = AsyncMock(return_value=[])
        count = AsyncMock(return_value=4)
        with (
            patch("turncall.storage.repositories.eval_repo.list_scenarios", rows),
            patch("turncall.storage.repositories.eval_repo.count_scenarios", count),
        ):
            result = await list_eval_scenarios(AUTH, AsyncMock(), page=1, limit=50)

        assert rows.await_args.kwargs["offset"] == 0
        assert result["total"] == 4


class TestTheFanOutStillSeesEverything:
    async def test_a_tag_run_reads_every_matching_scenario(self) -> None:
        """A suite that silently ran a page of itself reports a verdict for
        scenarios that never ran — worse than a slow query."""
        from turncall.api.v1.schemas.evals import CreateEvalRunRequest, EvalTarget

        listed = AsyncMock(return_value=[])
        body = CreateEvalRunRequest(
            tag="pre-publish", target=EvalTarget(type="agent_name", name="support")
        )
        with patch("turncall.storage.repositories.eval_repo.list_scenarios", listed):
            with pytest.raises(BadRequestError):  # empty tag, which is fine here
                await create_eval_run(body, AUTH, AsyncMock())

        assert listed.await_args.kwargs.get("limit") is None, (
            "the fan-out must not be handed a page"
        )
