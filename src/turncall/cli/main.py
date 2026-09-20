"""`turncall eval run | list | show` (#77).

Argument parsing and output. The exit-code rule lives in `verdict.py` and the
HTTP in `client.py`, so the part CI depends on is testable without a server.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from turncall.cli.client import Api, ApiError
from turncall.cli.verdict import (
    EXIT_ERRORED,
    EXIT_USAGE,
    exit_code,
    is_terminal,
    summary_line,
)

# How often to ask the API what happened, and how long to keep asking. A run is
# seconds to minutes; the API is local or one hop away, so a second is cheap and
# keeps the progress output feeling live.
POLL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 900


def _load_scenario_file(path: Path) -> dict[str, Any]:
    """A scenario file is the API request body, unchanged.

    No CLI-only fields and no YAML dialect: pipecat's parsers take a plain
    mapping, which is what a JSON object already is, so a second format would
    buy nothing and owe us push semantics, an identity rule and a sync story.
    """
    try:
        body = json.loads(path.read_text())
    except FileNotFoundError:
        raise ApiError(f"no such file: {path}") from None
    except json.JSONDecodeError as exc:
        raise ApiError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ApiError(f"{path}: expected a JSON object")
    if "definition" not in body:
        raise ApiError(f"{path}: needs a 'definition' (pipecat's scenario mapping)")
    body.setdefault("name", path.stem)
    return body


def _target_from_args(args: argparse.Namespace, api: Api) -> dict[str, Any] | None:
    """The target named on the command line, or None to fall back."""
    if args.agent_id:
        return {"type": "agent", "agent_id": args.agent_id}
    if args.agent_name:
        return {"type": "agent_name", "name": args.agent_name}
    if args.inline_agent:
        path = Path(args.inline_agent)
        try:
            return {"type": "inline", "agent": json.loads(path.read_text())}
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError(f"{path}: {exc}") from exc
    return None


def _default_target(api: Api, *, scenario_name: str | None, tag: str | None) -> dict:
    """The scenario's own `default_target`, when the command named none.

    For a tag, every matching scenario has to agree: the API takes one target
    per request, so silently picking the first scenario's would run the others
    against an agent nobody chose.
    """
    scenarios = api.list_scenarios(tag=tag) if tag else api.list_scenarios()
    if scenario_name:
        scenarios = [s for s in scenarios if s["name"] == scenario_name]
        if not scenarios:
            raise ApiError(f"no scenario named {scenario_name!r}")
    if not scenarios:
        raise ApiError(f"no scenarios carry the tag {tag!r}")

    targets = {json.dumps(s.get("default_target"), sort_keys=True) for s in scenarios}
    if len(targets) > 1:
        raise ApiError(
            "the matching scenarios have different default targets — "
            "pass --agent-id, --agent-name or --inline-agent"
        )
    target = scenarios[0].get("default_target")
    if not target:
        raise ApiError(
            "no target: pass --agent-id, --agent-name or --inline-agent, "
            "or give the scenario a default_target"
        )
    return target


def _scenario_id(api: Api, name: str) -> str:
    for scenario in api.list_scenarios():
        if scenario["name"] == name:
            return scenario["id"]
    raise ApiError(f"no scenario named {name!r}")


def _submit(api: Api, args: argparse.Namespace) -> dict[str, Any]:
    """Build the request body and post it. One request, one batch."""
    target = _target_from_args(args, api)

    if args.files:
        # Each file is its own run; one batch per file, since the API takes one
        # scenario per request and these are not stored to be tagged together.
        batches = []
        for raw in args.files:
            body = _load_scenario_file(Path(raw))
            payload = {
                "scenario": body,
                "target": target or _default_target(api, scenario_name=None, tag=None),
                "modality": args.modality,
                "iterations": args.iterations,
            }
            batches.append(api.create_run(payload))
        return {
            "batch_id": None,
            "runs": [run for batch in batches for run in batch["runs"]],
        }

    if args.tag:
        payload: dict[str, Any] = {"tag": args.tag}
        target = target or _default_target(api, scenario_name=None, tag=args.tag)
    else:
        payload = {"scenario_id": _scenario_id(api, args.scenario)}
        target = target or _default_target(api, scenario_name=args.scenario, tag=None)
    payload |= {
        "target": target,
        "modality": args.modality,
        "iterations": args.iterations,
    }
    return api.create_run(payload)


def _watch(api: Api, run_ids: list[str], *, timeout: int, quiet: bool) -> list[dict]:
    """Poll until every run is terminal, printing each as it lands.

    Streamed rather than summarised at the end: a suite of ten scenarios takes
    minutes, and a command that prints nothing until it is done looks hung.
    """
    pending = dict.fromkeys(run_ids)
    finished: dict[str, dict] = {}
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        for run_id in list(pending):
            run = api.get_run(run_id)
            if not is_terminal(run["status"]):
                continue
            finished[run_id] = run
            del pending[run_id]
            if not quiet:
                print(_run_line(run), flush=True)
        if pending:
            time.sleep(POLL_SECONDS)

    for run_id in pending:
        # Timed out: report what the row says rather than inventing a verdict.
        finished[run_id] = api.get_run(run_id)
    return [finished[run_id] for run_id in run_ids]


_MARK = {
    "passed": "PASS",
    "failed": "FAIL",
    "errored": "ERROR",
    "cancelled": "CANCELLED",
    "queued": "queued",
    "running": "running",
}


def _run_line(run: dict[str, Any]) -> str:
    status = str(run.get("status", "?"))
    counts = f"{run.get('passed_count', 0)}/{run.get('iterations', 0)}"
    line = f"{_MARK.get(status, status):9} {run.get('scenario_name', '?')} ({counts})"
    if run.get("error"):
        line += f" — {run['error']}"
    return line


def _cmd_run(args: argparse.Namespace) -> int:
    api = Api.from_env(args.base_url, args.api_key)
    batch = _submit(api, args)
    runs = batch.get("runs", [])
    if not runs:
        print("nothing was queued", file=sys.stderr)
        return EXIT_ERRORED

    if batch.get("batch_id") and not args.quiet:
        print(f"batch {batch['batch_id']}: {len(runs)} run(s)", flush=True)

    finished = _watch(
        api, [r["id"] for r in runs], timeout=args.timeout, quiet=args.quiet
    )
    print(summary_line(finished))
    return exit_code(finished)


def _cmd_list(args: argparse.Namespace) -> int:
    api = Api.from_env(args.base_url, args.api_key)
    if args.batch:
        batch = api.get_batch(args.batch)
        print(f"batch {batch['batch_id']}: {batch['status']}")
        for run in batch["runs"]:
            print("  " + _run_line(run))
        return exit_code(batch["runs"])
    for run in api.list_runs(status=args.status, limit=args.limit):
        print(f"{run['id']}  " + _run_line(run))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Print one run's transcript with the verdicts beside it."""
    api = Api.from_env(args.base_url, args.api_key)
    run = api.get_run(args.run_id)
    print(f"{run['scenario_name']}  {run['status']}  ({run['id']})")
    if run.get("error"):
        print(f"error: {run['error']}")

    for entry in run.get("results", []):
        print(f"\niteration {entry['iteration']}: {_verdict_word(entry)}")
        for turn in entry.get("transcript", []):
            role = turn.get("role", "?")
            print(f"  {role:9} {turn.get('content', '')}")
            # Audio runs carry what the judge read and what the agent said.
            if turn.get("text"):
                print(f"  {'(agent)':9} {turn['text']}")
        for failure in entry.get("failures", []):
            print(f"  ✗ {_failure_line(failure)}")
        for metric in entry.get("metrics", []):
            mark = "✓" if metric.get("passed") else "✗"
            print(f"  {mark} metric {metric['name']}: {metric.get('score')}")
    return exit_code([run])


def _verdict_word(entry: dict[str, Any]) -> str:
    if entry.get("error"):
        return f"errored — {entry['error']}"
    if entry.get("passed") is None:
        return "no verdict"
    return "passed" if entry["passed"] else "failed"


def _failure_line(failure: dict[str, Any]) -> str:
    if "turn_index" in failure:
        return (
            f"turn {failure['turn_index']} "
            f"{failure.get('event_name', '')}: {failure.get('reason', '')}"
        )
    return f"{failure.get('kind', '')} {failure.get('name', '')}: {failure.get('reason', '')}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turncall", description="TurnCall command line"
    )
    sub = parser.add_subparsers(dest="group", required=True)
    evals = sub.add_parser("eval", help="agent evals").add_subparsers(
        dest="command", required=True
    )

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", help="API base URL (env TURNCALL_API_URL)")
        p.add_argument("--api-key", help="API key (env TURNCALL_API_KEY)")

    run = evals.add_parser(
        "run", help="run a scenario, a tag, or scenario files, and exit on the verdict"
    )
    common(run)
    run.add_argument(
        "files", nargs="*", help="scenario JSON files (API request bodies)"
    )
    run.add_argument("--scenario", help="a stored scenario, by name")
    run.add_argument("--tag", help="every stored scenario carrying this tag")
    run.add_argument("--agent-id", help="target: a pinned agent id")
    run.add_argument("--agent-name", help="target: the published agent of this name")
    run.add_argument(
        "--inline-agent", help="target: a JSON file holding an agent config"
    )
    run.add_argument("--modality", default="text", choices=("text", "audio"))
    run.add_argument("--iterations", type=int, default=1)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--quiet", action="store_true", help="only the summary line")
    run.set_defaults(func=_cmd_run)

    listing = evals.add_parser("list", help="list runs, or one batch")
    common(listing)
    listing.add_argument("--batch", help="a batch id: prints its verdict")
    listing.add_argument("--status", help="filter by run status")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(func=_cmd_list)

    show = evals.add_parser("show", help="print a run's transcript and verdicts")
    common(show)
    show.add_argument("run_id")
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "run":
        chosen = [bool(args.files), bool(args.scenario), bool(args.tag)]
        if sum(chosen) != 1:
            print(
                "pass exactly one of: scenario files, --scenario, --tag",
                file=sys.stderr,
            )
            return EXIT_USAGE
    try:
        return int(args.func(args))
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERRORED


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
