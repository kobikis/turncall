"""The CLI's exit code, and the shape of what it prints (#77).

A pull request can be gated on agent behaviour only if something exits
non-zero when the agent regressed, so that rule gets the most cover — and it is
checked without a server, a database or a worker, because that is the point of
keeping it in its own module.

The distinction this file exists to protect: **failed and errored are not the
same thing**. A failure is a claim about the agent; an error means nobody could
tell. Both are non-zero, and a CI log that calls an outage a regression sends
someone hunting a bug that does not exist.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turncall.cli import main as cli
from turncall.cli.client import Api, ApiError
from turncall.cli.verdict import (
    EXIT_CANCELLED,
    EXIT_ERRORED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    exit_code,
    summarise,
    summary_line,
)

pytestmark = pytest.mark.unit


def _run(status, **over):
    base = {
        "id": over.pop("id", "run-1"),
        "status": status,
        "scenario_name": over.pop("name", "greets"),
        "passed_count": over.pop("passed", 0),
        "failed_count": over.pop("failed", 0),
        "iterations": over.pop("iterations", 1),
        "error": over.pop("error", None),
        "results": over.pop("results", []),
    }
    base.update(over)
    return base


class TestExitCode:
    def test_zero_only_when_every_run_passed(self) -> None:
        assert exit_code([_run("passed"), _run("passed")]) == EXIT_OK

    def test_a_failure_is_non_zero(self) -> None:
        assert exit_code([_run("passed"), _run("failed")]) == EXIT_FAILED

    def test_an_error_is_non_zero_but_not_a_failure(self) -> None:
        """ "Your agent regressed" and "we could not check" want different
        alerts, so a pipeline has to be able to branch on them."""
        assert exit_code([_run("passed"), _run("errored")]) == EXIT_ERRORED
        assert EXIT_ERRORED != EXIT_FAILED

    def test_a_failure_outranks_an_error(self) -> None:
        """A claim about the agent is louder than a claim about the harness."""
        assert exit_code([_run("failed"), _run("errored")]) == EXIT_FAILED

    def test_a_cancelled_run_is_its_own_code(self) -> None:
        assert exit_code([_run("cancelled")]) == EXIT_CANCELLED

    def test_an_empty_batch_is_not_success(self) -> None:
        """Reporting success for a batch that ran nothing is how a green
        pipeline stops meaning anything."""
        assert exit_code([]) == EXIT_ERRORED

    def test_a_run_still_going_is_its_own_code(self) -> None:
        """Not `errored` (#112): an errored run is terminal and means nobody
        could tell, while this one is still going and will reach a verdict
        nobody will read, because the pipeline has already gone red."""
        assert exit_code([_run("passed"), _run("running")]) == EXIT_TIMEOUT
        assert exit_code([_run("queued")]) == EXIT_TIMEOUT

    def test_a_regression_outranks_a_timeout(self) -> None:
        """A suite where one scenario regressed and another was slow has
        regressed — the same precedence failure already has over error."""
        assert exit_code([_run("failed"), _run("running")]) == EXIT_FAILED

    def test_an_outage_outranks_a_timeout(self) -> None:
        """ "We could never check" is a stronger claim than "we stopped
        waiting", and it is the one that needs someone to look at the judge."""
        assert exit_code([_run("errored"), _run("running")]) == EXIT_ERRORED

    def test_a_batch_that_finished_in_time_is_untouched(self) -> None:
        assert exit_code([_run("passed"), _run("passed")]) == EXIT_OK
        assert exit_code([_run("passed"), _run("cancelled")]) == EXIT_CANCELLED
        assert exit_code([]) == EXIT_ERRORED, "an empty batch is still an outage"


class TestSummary:
    def test_every_terminal_status_is_present_even_at_zero(self) -> None:
        """A summary that omits `errored` when it is zero trains people not to
        look for it when it is not."""
        counts = summarise([_run("passed")])
        assert set(counts) == {"passed", "failed", "errored", "cancelled"}

    def test_the_line_names_errors_separately(self) -> None:
        line = summary_line([_run("passed"), _run("failed"), _run("errored")])
        assert "1/3 passed" in line
        assert "1 failed" in line
        assert "could not check" in line, "an outage must not read as a regression"

    def test_a_clean_run_says_only_what_passed(self) -> None:
        assert summary_line([_run("passed"), _run("passed")]) == "2/2 passed"

    def test_the_line_names_the_runs_it_stopped_waiting_for(self) -> None:
        """Folding them into either verdict teaches people to read the line
        wrong, which is the argument `errored` already won (#112)."""
        line = summary_line([_run("passed"), _run("running"), _run("queued")])
        assert "1/3 passed" in line
        assert "2 still running (timed out waiting)" in line
        assert "could not check" not in line


class TestScenarioFiles:
    def test_a_file_is_the_api_body_unchanged(self, tmp_path: Path) -> None:
        """No CLI-only fields: the file is what the API already accepts, which
        is why there is one validator and nothing to keep in sync."""
        body = {
            "name": "books",
            "definition": {"turns": [{"user": "hi", "expect": []}]},
            "tool_policy": "mock_only",
        }
        path = tmp_path / "books.json"
        path.write_text(json.dumps(body))
        assert cli._load_scenario_file(path) == body

    def test_the_name_falls_back_to_the_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "cancels.json"
        path.write_text(json.dumps({"definition": {"turns": []}}))
        assert cli._load_scenario_file(path)["name"] == "cancels"

    def test_a_file_without_a_definition_is_refused_before_the_api(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"name": "nothing"}))
        with pytest.raises(ApiError, match="needs a 'definition'"):
            cli._load_scenario_file(path)

    def test_malformed_json_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        with pytest.raises(ApiError, match=r"broken\.json"):
            cli._load_scenario_file(path)


class TestUsage:
    def test_two_selections_at_once_is_a_usage_error(self) -> None:
        """Not a run against whichever the parser happened to see first."""
        code = cli.main(["eval", "run", "--scenario", "greets", "--tag", "pre-publish"])
        assert code == EXIT_USAGE

    def test_no_selection_at_all_is_a_usage_error(self) -> None:
        assert cli.main(["eval", "run"]) == EXIT_USAGE

    def test_a_missing_key_is_reported_not_raised(self, monkeypatch) -> None:
        monkeypatch.delenv("TURNCALL_API_KEY", raising=False)
        with pytest.raises(ApiError, match="TURNCALL_API_KEY"):
            Api.from_env(None, None)


class TestDefaultTarget:
    def test_the_scenarios_own_default_target_is_used(self) -> None:
        api = MagicMock()
        api.list_scenarios.return_value = [
            {"name": "greets", "default_target": {"type": "agent_name", "name": "sup"}}
        ]
        target = cli._default_target(api, scenario_name="greets", tag=None)
        assert target == {"type": "agent_name", "name": "sup"}

    def test_disagreeing_defaults_across_a_tag_are_refused(self) -> None:
        """One request carries one target, so picking the first scenario's
        would run the others against an agent nobody chose."""
        api = MagicMock()
        api.list_scenarios.return_value = [
            {"name": "a", "default_target": {"type": "agent_name", "name": "one"}},
            {"name": "b", "default_target": {"type": "agent_name", "name": "two"}},
        ]
        with pytest.raises(ApiError, match="different default targets"):
            cli._default_target(api, scenario_name=None, tag="pre-publish")

    def test_no_default_and_no_flag_says_what_to_pass(self) -> None:
        api = MagicMock()
        api.list_scenarios.return_value = [{"name": "greets", "default_target": None}]
        with pytest.raises(ApiError, match="--agent-id"):
            cli._default_target(api, scenario_name="greets", tag=None)


class TestProgressStreams:
    def test_each_run_prints_as_it_finishes(self, capsys) -> None:
        """A suite takes minutes; a command that prints nothing until the end
        looks hung."""
        api = MagicMock()
        # First poll: one done, one still going. Second: both done.
        api.get_run.side_effect = [
            _run("passed", id="a", name="first"),
            _run("running", id="b", name="second"),
            _run("failed", id="b", name="second", failed=1),
        ]
        with patch.object(cli.time, "sleep"):
            finished = cli._watch(api, ["a", "b"], timeout=10, quiet=False)

        out = capsys.readouterr().out
        assert "PASS" in out and "first" in out
        assert "FAIL" in out and "second" in out
        assert [r["status"] for r in finished] == ["passed", "failed"]

    def test_quiet_prints_nothing_per_run(self, capsys) -> None:
        api = MagicMock()
        api.get_run.side_effect = [_run("passed", id="a")]
        with patch.object(cli.time, "sleep"):
            cli._watch(api, ["a"], timeout=10, quiet=True)
        assert capsys.readouterr().out == ""


class TestShow:
    def test_a_transcript_prints_with_its_failures(self, capsys) -> None:
        api = MagicMock()
        api.get_run.return_value = _run(
            "failed",
            failed=1,
            results=[
                {
                    "iteration": 1,
                    "passed": False,
                    "transcript": [
                        {"role": "user", "content": "what are your hours?"},
                        {"role": "assistant", "content": "We close at nine."},
                    ],
                    "failures": [
                        {
                            "turn_index": 0,
                            "event_name": "llm_response",
                            "reason": "expected 'eight'",
                        }
                    ],
                }
            ],
        )
        with patch.object(cli.Api, "from_env", return_value=api):
            code = cli.main(["eval", "show", "run-1", "--api-key", "k"])

        out = capsys.readouterr().out
        assert "what are your hours?" in out
        assert "We close at nine." in out
        assert "expected 'eight'" in out
        assert code == EXIT_FAILED

    def test_an_audio_run_shows_both_what_was_heard_and_said(self, capsys) -> None:
        """The difference between them is the whole explanation when an audio
        run fails where a text run passed (#72)."""
        api = MagicMock()
        api.get_run.return_value = _run(
            "passed",
            passed=1,
            results=[
                {
                    "iteration": 1,
                    "passed": True,
                    "transcript": [
                        {
                            "role": "assistant",
                            "content": "we close at nine",
                            "text": "We close at 9.",
                        }
                    ],
                }
            ],
        )
        with patch.object(cli.Api, "from_env", return_value=api):
            cli.main(["eval", "show", "run-1", "--api-key", "k"])
        out = capsys.readouterr().out
        assert "we close at nine" in out and "We close at 9." in out


class TestTheDeadlineFitsTheBatch:
    """The old flat 900s was a *per-run* budget used as a whole-batch deadline
    (#98): ten scenarios behind a four-slot worker is three waves, so a healthy
    suite was abandoned and CI read `errored` as an outage."""

    def test_the_default_scales_with_how_many_runs_were_queued(self) -> None:
        args = cli.build_parser().parse_args(["eval", "run", "--tag", "pre-publish"])
        assert args.timeout is None, "computed from the batch, not a flat constant"

        seen = {}

        def _watch(_api, run_ids, *, timeout, quiet, batch_id=None):
            seen["timeout"] = timeout
            return [_run("passed", id=r) for r in run_ids]

        batch = {"batch_id": "b1", "runs": [{"id": f"r{i}"} for i in range(10)]}
        with (
            patch.object(cli.Api, "from_env", MagicMock()),
            patch.object(cli, "_submit", MagicMock(return_value=batch)),
            patch.object(cli, "_watch", _watch),
        ):
            assert cli._cmd_run(args) == EXIT_OK
        assert seen["timeout"] == 10 * cli.DEFAULT_TIMEOUT_PER_RUN_SECONDS

    def test_an_explicit_timeout_still_wins(self) -> None:
        args = cli.build_parser().parse_args(
            ["eval", "run", "--tag", "t", "--timeout", "30"]
        )
        seen = {}

        def _watch(_api, run_ids, *, timeout, quiet, batch_id=None):
            seen["timeout"] = timeout
            return [_run("passed", id=r) for r in run_ids]

        batch = {"batch_id": None, "runs": [{"id": "r1"}]}
        with (
            patch.object(cli.Api, "from_env", MagicMock()),
            patch.object(cli, "_submit", MagicMock(return_value=batch)),
            patch.object(cli, "_watch", _watch),
        ):
            cli._cmd_run(args)
        assert seen["timeout"] == 30

    def test_giving_up_says_so_instead_of_looking_like_an_outage(self, capsys) -> None:
        api = MagicMock()
        api.get_run.return_value = _run("running", id="a")
        finished = cli._watch(api, ["a"], timeout=0, quiet=True)

        err = capsys.readouterr().err
        assert "timed out" in err and "still going" in err
        assert "--timeout" in err, "the log has to say what to do about it"
        assert f"exit {EXIT_TIMEOUT}" in err, "the log and the code must agree"
        # No invented verdict: the row says what it says, and the code says the
        # command stopped waiting rather than that nobody could check (#112).
        assert finished[0]["status"] == "running"
        assert exit_code(finished) == EXIT_TIMEOUT


class TestThePollingIsCheap:
    def test_a_batch_is_one_request_per_tick_not_one_per_run(self) -> None:
        """`GET /v1/eval-runs/batches/{id}` was built for this question (#75)
        and carries no transcripts; `get_run` returns all three snapshots."""
        api = MagicMock()
        api.get_batch.side_effect = [
            {"runs": [_run("running", id="a"), _run("running", id="b")]},
            {"runs": [_run("passed", id="a"), _run("failed", id="b")]},
        ]
        with patch.object(cli.time, "sleep"):
            finished = cli._watch(
                api, ["a", "b"], timeout=10, quiet=True, batch_id="b1"
            )

        assert api.get_batch.call_count == 2, "two ticks, two requests, four runs"
        assert api.get_run.call_count == 0, "no transcripts pulled while polling"
        assert [r["status"] for r in finished] == ["passed", "failed"]

    def test_the_interval_widens_and_is_capped(self) -> None:
        """A run takes minutes; a poll a second is thousands of requests to
        learn a status string."""
        api = MagicMock()
        api.get_run.return_value = _run("running", id="a")
        slept: list[float] = []

        with patch.object(cli.time, "sleep", slept.append):
            # A deadline in the future for a handful of ticks, then done.
            ticks = iter([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1_000_000])
            with patch.object(cli.time, "monotonic", lambda: next(ticks)):
                cli._watch(api, ["a"], timeout=10, quiet=True)

        assert slept[0] == cli.POLL_SECONDS
        assert slept[1] > slept[0], "it widens"
        assert max(slept) <= cli.MAX_POLL_SECONDS, "and is capped"
        assert slept == sorted(slept), "monotonically"
