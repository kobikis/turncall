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
    EXIT_TIMEOUT,
    EXIT_USAGE,
    exit_code,
    is_terminal,
    summary_line,
)

# How often to ask the API what happened. The first tick is quick so a short
# suite feels live; it then widens, because a run takes minutes and sub-second
# resolution buys nothing but requests (#98).
POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 10.0
# The deadline is per **run**, multiplied by how many were queued. The old flat
# 900s was a per-run budget used as a whole-batch one, so ten scenarios behind a
# four-slot worker gave up on a healthy suite and CI read it as an outage. A CI
# job has its own overall timeout; this one only needs to not be the first to
# fire. `--timeout` is the exception, and is the whole batch's.
DEFAULT_TIMEOUT_PER_RUN_SECONDS = 900


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


# What the API replaces a secret with in any config it returns.
_MASK = "***"


def _default_target(api: Api, *, scenario_name: str | None, tag: str | None) -> dict:
    """The scenario's own `default_target`, when the command named none.

    For a tag, every matching scenario has to agree: the API takes one target
    per request, so silently picking the first scenario's would run the others
    against an agent nobody chose.
    """
    # One page big enough to be every scenario a tag could run: the API
    # refuses a tag matching more than its own fan-out cap (#97), so agreeing
    # on a target across a *subset* is a state that cannot arise (#99).
    page = {"limit": 500}
    scenarios = (
        api.list_scenarios(tag=tag, **page) if tag else api.list_scenarios(**page)
    )
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
    if _MASK in json.dumps(target):
        # The API masks the credentials in an inline target (#91), so what
        # came back cannot be run. Better to say that than to submit `***` as
        # an API key and let the provider report it three layers down.
        raise ApiError(
            "this scenario's default_target holds masked credentials — "
            "pass --agent-id, --agent-name, or --inline-agent with the config"
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


def _poll(api: Api, pending: dict[str, Any], batch_id: str | None) -> list[dict]:
    """The pending runs' current state, in as few requests as possible.

    A batch is one request however many runs it holds — #75 built
    `GET /v1/eval-runs/batches/{id}` for exactly this question, and it answers
    it without the transcripts and the three snapshots that `get_run` carries.
    Loose runs (a pile of scenario files) have no batch to ask about.
    """
    if batch_id:
        runs = api.get_batch(batch_id).get("runs", [])
        return [run for run in runs if str(run.get("id")) in pending]
    return [api.get_run(run_id) for run_id in list(pending)]


def _watch(
    api: Api,
    run_ids: list[str],
    *,
    timeout: int,
    quiet: bool,
    batch_id: str | None = None,
) -> list[dict]:
    """Poll until every run is terminal, printing each as it lands.

    Streamed rather than summarised at the end: a suite of ten scenarios takes
    minutes, and a command that prints nothing until it is done looks hung.
    """
    pending = dict.fromkeys(str(run_id) for run_id in run_ids)
    finished: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    delay = POLL_SECONDS

    while pending and time.monotonic() < deadline:
        for run in _poll(api, pending, batch_id):
            run_id = str(run.get("id"))
            if run_id not in pending or not is_terminal(run["status"]):
                continue
            finished[run_id] = run
            del pending[run_id]
            if not quiet:
                print(_run_line(run), flush=True)
        if pending:
            time.sleep(delay)
            delay = min(delay * 2, MAX_POLL_SECONDS)

    if pending:
        # Say it out loud. The runs are still going, so the verdict below is
        # `errored` — "nobody could check" is the truth when the command
        # stopped waiting, but a log that does not say why reads as an outage.
        print(
            f"timed out after {timeout}s with {len(pending)} run(s) still going; "
            f"they are still running — raise --timeout, or read the verdict later "
            f"with `turncall eval list` (exit {EXIT_TIMEOUT})",
            file=sys.stderr,
            flush=True,
        )
    for run_id in pending:
        # Report what the row says rather than inventing a verdict.
        finished[run_id] = api.get_run(run_id)
    return [finished[str(run_id)] for run_id in run_ids]


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


def _print_warnings(run: dict[str, Any]) -> None:
    """Things that are not verdicts but change how the verdict reads (#96).

    A mock that can never fire makes a green run mean less than it looks like
    it does, and the run is where the person who wrote the scenario is looking.
    """
    for warning in run.get("warnings") or []:
        print(f"warning: {warning.get('message') or warning.get('code')}")


def _cmd_run(args: argparse.Namespace) -> int:
    api = Api.from_env(args.base_url, args.api_key)
    batch = _submit(api, args)
    runs = batch.get("runs", [])
    if not runs:
        print("nothing was queued", file=sys.stderr)
        return EXIT_ERRORED

    if batch.get("batch_id") and not args.quiet:
        print(f"batch {batch['batch_id']}: {len(runs)} run(s)", flush=True)

    # Per run, not per batch: the queue is served a few runs at a time, so a
    # ten-scenario suite is several waves of one run's worth of work.
    timeout = args.timeout or DEFAULT_TIMEOUT_PER_RUN_SECONDS * len(runs)
    finished = _watch(
        api,
        [r["id"] for r in runs],
        timeout=timeout,
        quiet=args.quiet,
        batch_id=batch.get("batch_id"),
    )
    for run in finished:
        _print_warnings(run)
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
    _print_warnings(run)

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
    run.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="seconds to wait for the whole batch "
        f"(default: {DEFAULT_TIMEOUT_PER_RUN_SECONDS}s per queued run)",
    )
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
