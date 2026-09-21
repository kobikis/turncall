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

pytestmark = pytest.mark.unit

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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


class TestThePagingBoundsAreValidated:
    """`page=0` used to become `offset=-50`, which Postgres refuses — a 500 for
    a caller who typed a wrong number, from an authenticated endpoint."""

    def test_the_declared_bounds_reject_a_zero_or_negative_page(self) -> None:
        from turncall.api.v1.evals import MAX_PAGE_SIZE, Page, PageSize

        def bounds(annotated):
            return {
                type(c).__name__.lower(): getattr(c, type(c).__name__.lower())
                for c in annotated.__metadata__[0].metadata
            }

        assert bounds(Page) == {"ge": 1}, "page=0 becomes a negative OFFSET"
        assert bounds(PageSize) == {"ge": 1, "le": MAX_PAGE_SIZE}

    @pytest.mark.asyncio
    async def test_a_page_never_produces_a_negative_offset(self) -> None:
        """The arithmetic the bound protects."""
        rows = AsyncMock(return_value=[])
        count = AsyncMock(return_value=0)
        with (
            patch("turncall.storage.repositories.eval_repo.list_runs", rows),
            patch("turncall.storage.repositories.eval_repo.count_runs", count),
        ):
            await list_eval_runs(AUTH, AsyncMock(), page=1, limit=50)
        assert rows.await_args.kwargs["offset"] == 0


class TestTheCliReadsEveryScenario:
    """A target agreed across a subset runs the rest against an agent nobody
    chose — so the CLI pages to the end rather than asking for one big page."""

    def test_it_follows_pages_until_a_short_one(self) -> None:
        from unittest.mock import MagicMock

        from turncall.cli import main as cli

        api = MagicMock()
        api.list_scenarios.side_effect = [
            [{"name": f"s{i}"} for i in range(cli._PAGE_SIZE)],
            [{"name": "last"}],
        ]
        assert len(cli._all_scenarios(api)) == cli._PAGE_SIZE + 1
        assert api.list_scenarios.call_args_list[1].kwargs["page"] == 2

    def test_it_asks_for_a_page_size_the_server_allows(self) -> None:
        from turncall.api.v1.evals import MAX_PAGE_SIZE
        from turncall.cli import main as cli

        assert cli._PAGE_SIZE <= MAX_PAGE_SIZE, (
            "asking for more than the server's ceiling is a 422, not a big page"
        )

    def test_an_endless_listing_raises_instead_of_hanging(self) -> None:
        from unittest.mock import MagicMock

        from turncall.cli import main as cli
        from turncall.cli.client import ApiError

        api = MagicMock()
        api.list_scenarios.return_value = [{"name": "x"}] * cli._PAGE_SIZE
        with pytest.raises(ApiError, match="narrow the tag"):
            cli._all_scenarios(api)
