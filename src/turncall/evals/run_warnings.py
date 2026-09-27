"""What the author of a run has to be told, that is not a verdict.

Three things a run can only say in the run itself (#96, #119). None of them is
a failure and none is an error: a mock that can never fire, a judge that is not
the one that decided this scenario's last verdict, and a worker older than the
code it is running. They were worker log lines, which is the container's
stdout — and nobody reading a run back has ever seen it.

Each returns a list so a caller can concatenate them without caring which
fired, and each is safe to call on any run.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger

from turncall.evals.targets import ResolvedTarget


def warn_unmatched_mocks(
    tool_mocks: dict[str, Any], target: ResolvedTarget, *, run_id: UUID
) -> list[dict[str, Any]]:
    """Say so when a mock names a tool this run can never call.

    An eval builds its pipeline through `build_call_pipeline`, which takes no
    MCP manager, so an agent's MCP servers are neither connected nor
    discovered: nothing is contacted (which is the point), but the model is
    never shown those tools either, and a mock keyed to one sits there meaning
    nothing. A typo in a mock's name looks exactly the same. Silence is the
    worst of the three, since a mock that never fires reads as a tool that was
    never called.

    ponytail: a warning, not a rejection — the agent can be edited after the
    scenario was written, and failing a queued run over a stale mock helps
    nobody. Advertising mocked-but-undiscovered tools to the model is the real
    fix, and needs a schema the mock does not carry today (#74's inline targets
    are the sanctioned way to give an eval a tool surface of its own).
    """
    if not tool_mocks:
        return []
    from turncall.domain.models import BUILTIN_TOOL_NAMES

    known = {t.name for t in target.config.tools} | set(BUILTIN_TOOL_NAMES)
    unmatched = sorted(set(tool_mocks) - known)
    if not unmatched:
        return []
    servers = len(target.config.mcp_servers)
    logger.warning(
        "eval_mock_matches_no_tool",
        run_id=str(run_id),
        tools=unmatched,
        mcp_servers=servers,
    )
    # Returned as well as logged (#96): the log is the worker's container and
    # the author reads the run. The MCP server count is the valuable half —
    # it is what says "this is the MCP limitation, not your typo".
    return [
        {
            "code": "mock_matches_no_tool",
            "message": (
                f"{len(unmatched)} mock(s) name a tool this run cannot call: "
                + ", ".join(unmatched)
                + (
                    f". The agent has {servers} MCP server(s), whose tools an "
                    "eval never connects or advertises — a mock naming one can "
                    "never fire."
                    if servers
                    else ". Check the tool name against the agent's tools."
                )
            ),
            "tools": unmatched,
            "mcp_servers": servers,
        }
    ]


_JUDGE_KEYS = (
    "judge_provider",
    "judge_model",
    "judge_temperature",
    "judge_service",
    "judge_factory",
)


async def warn_judge_changed(
    session: Any, run: Any, harness: dict[str, Any]
) -> list[dict[str, Any]]:
    """Say so when this scenario's last verdict came from a different judge.

    One indexed query against `scenario_id`, and only for a stored scenario: an
    inline one (#77) has no history to differ from, and neither does the first
    run of anything.
    """
    from turncall.storage.repositories import eval_repo

    if run.scenario_id is None:
        return []
    previous = await eval_repo.last_judged_harness(
        session, scenario_id=run.scenario_id, excluding=run.id
    )
    if not isinstance(previous, dict) or not previous:
        # Nothing to compare against: no earlier verdict, or a snapshot that is
        # not a mapping — `harness_config` is JSONB and a row written by hand
        # can hold anything.
        return []

    # Only keys the previous snapshot actually recorded. `judge_provider` and
    # `judge_temperature` arrived with this slice, so every older row is
    # missing them — comparing against that absence would announce a judge
    # change on the next run of every scenario in the database.
    changed = {
        key: {"previous": previous.get(key), "current": harness.get(key)}
        for key in _JUDGE_KEYS
        if key in previous and previous.get(key) != harness.get(key)
    }
    if not changed:
        return []
    return [
        {
            "code": "judge_changed",
            "message": (
                "this scenario's previous verdict was decided by a different "
                "judge: "
                + "; ".join(
                    f"{key} {entry['previous']!r} -> {entry['current']!r}"
                    for key, entry in changed.items()
                )
                + ". A result that moved may be the judge moving, not the agent."
            ),
            "judge": changed,
        }
    ]


def warn_worker_stale(harness: dict[str, Any]) -> list[dict[str, Any]]:
    """Say so when the worker is older than the code it is running.

    The failure this exists for is silent by construction: the run succeeds,
    the verdict looks ordinary, and the feature the scenario depends on was
    simply not in the process. It belongs on the run rather than in a log,
    because the log is not what anyone reads when a result surprises them.
    """
    if not harness.get("worker_stale"):
        return []
    return [
        {
            "code": "worker_stale",
            "message": (
                "this run was executed by a worker started at "
                f"{harness.get('worker_started_at')}, before the code it runs "
                f"was last changed ({harness.get('worker_source_mtime')}). "
                "Anything added since is not in this result — restart the "
                "eval worker."
            ),
            "worker_started_at": harness.get("worker_started_at"),
            "worker_source_mtime": harness.get("worker_source_mtime"),
        }
    ]
