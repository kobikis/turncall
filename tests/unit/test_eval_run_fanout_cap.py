"""One request must not be able to queue unbounded paid LLM work (#97).

`iterations` was capped; the fan-out was not. A `tag` matches every scenario
carrying it, so one POST with a popular tag and `iterations: 50` queued
`scenarios x iterations` full conversations — each an agent pipeline plus a
persona LLM plus a judge — with nothing between the request and the bill.

Refused, never truncated: a batch that quietly ran 50 of 200 scenarios reports
a verdict for a suite that never ran, which is the same disease as the empty
batch that reports "0 failures" forever.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.api.errors import BadRequestError
from turncall.api.v1.evals import create_eval_run
from turncall.api.v1.schemas.evals import CreateEvalRunRequest, EvalTarget
from turncall.config.settings import EvalSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CAP = EvalSettings().max_scenarios_per_request


def _scenario(i: int):
    return SimpleNamespace(
        id=uuid4(),
        name=f"scenario-{i}",
        kind="script",
        definition={"turns": [{"user": "hi", "expect": [{"event": "llm_response"}]}]},
        schema_version="pipecat-1.11",
        tool_mocks={},
        tool_policy="mock_only",
    )


def _request(*, tag="pre-publish", iterations=1):
    return CreateEvalRunRequest(
        tag=tag,
        iterations=iterations,
        target=EvalTarget(type="agent_name", name="support"),
    )


async def _create(body, scenarios):
    """The endpoint, with the repository and the queue faked."""
    created = []

    async def create_run(_session, **kwargs):
        run = SimpleNamespace(
            id=uuid4(),
            scenario_id=kwargs["scenario_id"],
            scenario_name=kwargs["scenario_name"],
            status="queued",
        )
        created.append(run)
        return run

    with (
        patch(
            "turncall.storage.repositories.eval_repo.list_scenarios",
            AsyncMock(return_value=scenarios),
        ),
        patch("turncall.storage.repositories.eval_repo.create_run", create_run),
        patch("turncall.api.v1.evals._queue_one", AsyncMock(return_value="queued")),
    ):
        result = await create_eval_run(
            body,
            SimpleNamespace(project_id=uuid4()),
            AsyncMock(),
        )
    return result, created


class TestTheFanOutIsCapped:
    async def test_a_tag_over_the_cap_is_refused(self) -> None:
        scenarios = [_scenario(i) for i in range(CAP + 1)]
        with pytest.raises(BadRequestError) as caught:
            await _create(_request(iterations=50), scenarios)
        message = str(caught.value)
        assert str(CAP + 1) in message, "the refusal must say how many it matched"
        assert str(CAP) in message
        assert str((CAP + 1) * 50) in message, "and what that would have cost"

    async def test_nothing_is_queued_when_it_is_refused(self) -> None:
        """The rejection lands before the first row is written — a half-queued
        batch is the truncation this exists to avoid."""
        create_run = AsyncMock()
        with (
            patch(
                "turncall.storage.repositories.eval_repo.list_scenarios",
                AsyncMock(return_value=[_scenario(i) for i in range(CAP + 1)]),
            ),
            patch("turncall.storage.repositories.eval_repo.create_run", create_run),
            pytest.raises(BadRequestError),
        ):
            await create_eval_run(
                _request(), SimpleNamespace(project_id=uuid4()), AsyncMock()
            )
        assert create_run.await_count == 0

    async def test_a_tag_at_the_cap_runs(self) -> None:
        scenarios = [_scenario(i) for i in range(CAP)]
        result, created = await _create(_request(), scenarios)
        assert len(created) == CAP
        assert len(result["data"]["runs"]) == CAP

    async def test_an_empty_tag_is_still_refused(self) -> None:
        with pytest.raises(BadRequestError) as caught:
            await _create(_request(tag="typo"), [])
        assert "no scenarios carry the tag" in str(caught.value)
