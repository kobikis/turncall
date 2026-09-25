"""Which code executed a run (#119).

The failure this exists for is silent by construction. A scenario's judge was
set, stored correctly and ignored for a day: the worker process had been
started before the feature existed, and `docker compose` bind-mounts the
source, so the files were current and the loaded modules were not. Every run
looked ordinary. Diagnosing it took a database query.

A run cannot be attributed to a version it does not record — the same argument
`harness_config` already makes for the judge, one layer down.
"""

import time

import pytest

from turncall.evals import build

pytestmark = pytest.mark.unit


class TestTheSnapshotSaysWhatRanIt:
    def test_it_records_the_version_and_when_the_process_started(self) -> None:
        snapshot = build.worker_build()
        assert snapshot["worker_version"]
        # ISO, like every other timestamp a run carries.
        assert snapshot["worker_started_at"].endswith("+00:00")

    def test_source_older_than_the_process_is_not_stale(self, monkeypatch) -> None:
        """A packaged deployment bakes its source at image build, so the
        mtimes precede every process start. This must stay quiet there, or the
        warning is noise everywhere it matters least."""
        monkeypatch.setattr(build, "_STARTED_AT", time.time())
        monkeypatch.setattr(build, "newest_source_mtime", lambda: time.time() - 3600)
        assert build.worker_build()["worker_stale"] is False

    def test_source_edited_after_the_process_started_is_stale(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(build, "_STARTED_AT", time.time() - 3600)
        monkeypatch.setattr(build, "newest_source_mtime", lambda: time.time())
        snapshot = build.worker_build()
        assert snapshot["worker_stale"] is True
        assert snapshot["worker_source_mtime"] > snapshot["worker_started_at"]

    def test_a_file_written_during_startup_is_not_a_code_change(
        self, monkeypatch
    ) -> None:
        """Seconds of grace, deliberately small: the case this catches is
        hours old, never seconds."""
        now = time.time()
        monkeypatch.setattr(build, "_STARTED_AT", now)
        monkeypatch.setattr(build, "newest_source_mtime", lambda: now + 1)
        assert build.worker_build()["worker_stale"] is False

    def test_the_walk_finds_this_very_file(self) -> None:
        """Guards the path: a `_PACKAGE` pointing at the wrong directory would
        report 0 forever and never warn about anything."""
        assert build.newest_source_mtime() > 0


class TestTheRunSaysSo:
    def test_a_stale_worker_warns_on_the_run(self) -> None:
        from turncall.evals.runner import _warn_worker_stale

        warnings = _warn_worker_stale(
            {
                "worker_stale": True,
                "worker_started_at": "2026-09-25T05:59:00+00:00",
                "worker_source_mtime": "2026-09-25T08:36:00+00:00",
            }
        )
        assert [w["code"] for w in warnings] == ["worker_stale"]
        # Both times, because "restart it" is only actionable once you can see
        # the gap you are closing.
        assert "05:59" in warnings[0]["message"]
        assert "08:36" in warnings[0]["message"]

    def test_a_current_worker_says_nothing(self) -> None:
        from turncall.evals.runner import _warn_worker_stale

        assert _warn_worker_stale({"worker_stale": False}) == []

    def test_an_older_run_without_the_field_says_nothing(self) -> None:
        """Every row written before this has no such key, and reading its
        absence as staleness would warn about every run in the database."""
        from turncall.evals.runner import _warn_worker_stale

        assert _warn_worker_stale({}) == []

    def test_the_harness_snapshot_carries_it(self) -> None:
        from turncall.evals.runner import harness_config

        snapshot = harness_config(None)
        assert "worker_version" in snapshot
        assert "worker_stale" in snapshot
