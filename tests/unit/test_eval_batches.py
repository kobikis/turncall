"""Fanning a run out across a tag, and reading one verdict back (#75).

Grouping is a tag array on the scenario plus a batch id on the run —
deliberately no suite table, no join table. What that leaves to get right is
the fan-out at submission and the aggregate on the way back, which is what the
CLI's single exit code will rest on.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.evals import CreateEvalRunRequest, EvalTarget
from turncall.domain.enums import EvalRunStatus
from turncall.evals.runner import batch_outcome

pytestmark = pytest.mark.unit


def _target():
    return EvalTarget(type="agent", agent_id=uuid4())


def _run(status, *, passed=0, failed=0, name="greets"):
    return SimpleNamespace(
        id=uuid4(),
        scenario_id=uuid4(),
        scenario_name=name,
        status=status,
        passed_count=passed,
        failed_count=failed,
        iterations=passed + failed or 1,
        error=None,
    )


class TestSelection:
    def test_a_tag_is_accepted_instead_of_a_scenario(self) -> None:
        body = CreateEvalRunRequest(tag="pre-publish", target=_target())
        assert body.tag == "pre-publish" and body.scenario_id is None

    def test_both_at_once_is_rejected(self) -> None:
        """Two selections mean two different runs; guessing which was meant is
        how a caller gets a batch they did not ask for."""
        with pytest.raises(ValidationError, match="exactly one"):
            CreateEvalRunRequest(
                scenario_id=uuid4(), tag="pre-publish", target=_target()
            )

    def test_neither_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            CreateEvalRunRequest(target=_target())


class TestBatchOutcome:
    """The batch verdict is the run verdict one level out, for the same
    reasons: `errored` is not a kind of pass, at either level."""

    def test_every_run_passing_passes_the_batch(self) -> None:
        batch_id = uuid4()
        outcome = batch_outcome(
            batch_id, [_run("passed", passed=3), _run("passed", passed=1)]
        )
        assert outcome["status"] is EvalRunStatus.PASSED
        assert (outcome["total"], outcome["passed_count"]) == (2, 2)
        assert outcome["batch_id"] == batch_id

    def test_one_failing_run_fails_the_batch(self) -> None:
        outcome = batch_outcome(
            uuid4(), [_run("passed", passed=1), _run("failed", failed=1)]
        )
        assert outcome["status"] is EvalRunStatus.FAILED
        assert (outcome["passed_count"], outcome["failed_count"]) == (1, 1)

    def test_a_run_still_in_flight_keeps_the_batch_running(self) -> None:
        """A verdict read before every run finished would be a lie the caller
        cannot detect — and the CLI would exit on it."""
        for in_flight in ("queued", "running"):
            outcome = batch_outcome(
                uuid4(), [_run("failed", failed=1), _run(in_flight)]
            )
            assert outcome["status"] is EvalRunStatus.RUNNING

    def test_nothing_reaching_a_verdict_errors_the_batch(self) -> None:
        """Not `passed`. A batch where every run errored proved nothing about
        the agent, and reporting that green is how a suite loses its
        audience."""
        outcome = batch_outcome(uuid4(), [_run("errored"), _run("errored")])
        assert outcome["status"] is EvalRunStatus.ERRORED
        assert (outcome["passed_count"], outcome["failed_count"]) == (0, 0)

    def test_an_errored_run_beside_a_passing_one_does_not_fail_the_batch(self) -> None:
        """An errored run counts toward neither rate, the same rule an errored
        iteration follows inside a run."""
        outcome = batch_outcome(uuid4(), [_run("passed", passed=1), _run("errored")])
        assert outcome["status"] is EvalRunStatus.PASSED
        assert outcome["counts"] == {"passed": 1, "errored": 1}

    def test_a_cancelled_batch_reads_as_cancelled(self) -> None:
        outcome = batch_outcome(uuid4(), [_run("cancelled"), _run("cancelled")])
        assert outcome["status"] is EvalRunStatus.CANCELLED

    def test_the_counts_are_runs_not_iterations(self) -> None:
        """A batch of ten scenarios reads 9/10; each run still reports its own
        7/10 inside."""
        runs = [_run("passed", passed=7, failed=0) for _ in range(9)]
        runs.append(_run("failed", passed=2, failed=5))
        outcome = batch_outcome(uuid4(), runs)
        assert (outcome["passed_count"], outcome["failed_count"]) == (9, 1)
        assert outcome["total"] == 10
