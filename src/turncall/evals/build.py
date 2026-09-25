"""Which code executed a run, and whether it was the code on disk (#119).

`harness_config` already records the judge, because a verdict cannot be
compared across time without knowing which model answered. The same argument
applies one layer down and was missing: a run executed by a worker process
older than the code it is running produces a result nobody can attribute. That
is not hypothetical — a scenario's judge was set, stored correctly, and ignored
for a day, because the worker had been started before the feature existed and
`docker compose` bind-mounts the source. Diagnosing it took a database.

Two facts make that visible on the run itself: the version the worker is, and
whether its source has been edited since it started.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

# When this process loaded the code it is running. Module import is as close to
# "process start" as the code can honestly claim, and the difference is the
# interpreter's own startup.
_STARTED_AT = time.time()

# A file written during startup — a .pyc, an editor's save landing as the
# worker boots — should not read as a code change. Seconds, deliberately small:
# the case this catches is hours old, never seconds.
_GRACE_S = 5.0

_PACKAGE = Path(__file__).resolve().parent.parent


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def newest_source_mtime() -> float:
    """When the loaded package was last edited on disk.

    Walked rather than cached: the point is to notice an edit that happened
    *after* this process started, which a value computed once would never see.
    One walk per run start is nothing beside the conversation it precedes.
    """
    newest = 0.0
    for root, _dirs, files in os.walk(_PACKAGE):
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                newest = max(newest, os.stat(Path(root) / name).st_mtime)
            except OSError:  # pragma: no cover - a file vanishing mid-walk
                continue
    return newest


def _version() -> str:
    from importlib.metadata import version

    try:
        return version("turncall")
    except Exception:  # pragma: no cover - packaging metadata is always there
        return "unknown"


def worker_build() -> dict:
    """What executed this run, for the run's harness snapshot.

    `stale` is the useful one. A packaged deployment bakes its source at image
    build, so the mtimes are older than every process start and this stays
    False; a bind-mounted dev worker that has been left running while the code
    moved underneath it reports True, which is the whole case.
    """
    newest = newest_source_mtime()
    return {
        "worker_version": _version(),
        "worker_started_at": _iso(_STARTED_AT),
        "worker_source_mtime": _iso(newest) if newest else None,
        "worker_stale": bool(newest and newest > _STARTED_AT + _GRACE_S),
    }
