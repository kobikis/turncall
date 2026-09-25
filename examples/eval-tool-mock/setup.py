"""Eval Tool Mock Example — a scenario that books nothing

Demonstrates TurnCall's eval tool mocking (#71):
  1. Creates a project, an API key and an agent with a **webhook tool**
  2. Points that webhook at an unroutable URL, on purpose
  3. Runs a scenario whose `tool_mocks` answer for the tool — it passes,
     which proves the real webhook was never called
  4. Runs the same conversation with the *booking refused* mock — a second
     scenario, because "it succeeds" and "it fails" are two different tests
  5. Runs it once more with no mock at all — refused, and the run is
     `errored` naming the tool, which is what `mock_only` means

Prerequisites:
  1. Stack running: `make docker-up-local && make migrate-local`
  2. .env with PLATFORM_API_KEY and OPENAI_API_KEY

Usage:
  ./examples/eval-tool-mock/run.sh
"""

import argparse
import os
import sys
import time

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")

BASE_URL = os.environ.get("TURNCALL_API_URL", "http://localhost:8090")

# Unroutable by design: `.invalid` is reserved by RFC 2606 and resolves
# nowhere. If a mock ever failed to intercept, the tool call would fail
# against this host and the run would say so — which is what makes the
# passing run below evidence rather than a claim.
NEVER_CALLED = "https://book.invalid/reserve"

AGENT = {
    "name": "nonna-bookings",
    "config": {
        "system_prompt": (
            "You are the phone host for Nonna, an Italian restaurant. When a "
            "caller asks for a table, call book_table with the party size and "
            "the time, then tell them what happened. If the booking is "
            "refused, say so plainly and offer to take a different time. "
            "Never claim a table is booked unless the tool said so."
        ),
        "first_message": "Nonna, good evening.",
        "tools": [
            {
                "name": "book_table",
                "description": "Reserve a table. Returns the booking or a refusal.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "party_size": {"type": "integer"},
                        "time": {"type": "string"},
                    },
                    "required": ["party_size", "time"],
                },
                "webhook_url": NEVER_CALLED,
            }
        ],
    },
}

# One conversation, three scenarios. The turns are identical — only the mock
# changes, which is the whole point: a mock belongs to the scenario, so
# "the booking succeeds" and "the booking fails" are two tests, not one test
# with a flag.
TURNS = [
    {
        "user": "Hi, can I book a table for four at eight tonight?",
        "expect": [{"event": "function_call", "calls": [{"name": "book_table"}]}],
    }
]

SCENARIOS = [
    {
        "name": "booking-confirmed",
        "tags": ["bookings"],
        "definition": {
            "name": "booking-confirmed",
            "turns": [
                *TURNS,
                {
                    "user": "Lovely, thank you.",
                    "expect": [
                        {"event": "llm_response", "text_contains": "B-1234"},
                    ],
                },
            ],
        },
        # The canned result the model is handed instead of the webhook firing.
        "tool_mocks": {"book_table": {"status": "confirmed", "reference": "B-1234"}},
        "tool_policy": "mock_only",
    },
    {
        "name": "booking-refused",
        "tags": ["bookings"],
        "definition": {
            "name": "booking-refused",
            "turns": [
                *TURNS,
                {
                    "user": "Oh. Is there anything else?",
                    "expect": [
                        # The failure worth testing is the agent claiming a
                        # table it does not have.
                        {"event": "llm_response", "text_excludes": "confirmed"},
                    ],
                },
            ],
        },
        "tool_mocks": {"book_table": {"status": "refused", "reason": "fully booked"}},
        "tool_policy": "mock_only",
    },
    {
        "name": "no-mock-at-all",
        "tags": ["bookings"],
        "definition": {"name": "no-mock-at-all", "turns": TURNS},
        # Deliberately empty: `mock_only` refuses a tool nothing answers for,
        # and the run is `errored` naming it. That is the default because the
        # alternative is an eval suite that books real tables every night.
        "tool_mocks": {},
        "tool_policy": "mock_only",
    },
]


def api(method: str, path: str, data: dict | None = None, token: str = "") -> dict:
    """Make an API call and return the response."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        # Token-less calls are the platform-gated bootstrap endpoints.
        if not PLATFORM_KEY:
            raise SystemExit(
                "PLATFORM_API_KEY is not set — add it to .env (see env.example)"
            )
        headers["X-Platform-Key"] = PLATFORM_KEY

    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        resp = client.request(method, path, json=data, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


def wait_for(run_id: str, token: str, timeout_s: int) -> dict:
    """Poll one run until it reaches a verdict.

    A worker executes runs, never the API process (ADR-0004), so a queued run
    is the normal first answer. `?view=summary` is the cheap read: status and
    counts without every iteration's transcript.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = api("GET", f"/v1/eval-runs/{run_id}?view=summary", token=token)["data"]
        if run["status"] in ("passed", "failed", "errored", "cancelled"):
            return run
        time.sleep(2)
    raise SystemExit(f"run {run_id} did not finish within {timeout_s}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the eval tool-mock example")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for each run (default: 180)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall Eval Tool Mock Example")
    print("=" * 60)

    print("\n1. Creating project...")
    project_id = api("POST", "/v1/projects", {"name": "eval-tool-mock-demo"})["data"][
        "id"
    ]
    print(f"   Project: {project_id}")

    print("\n2. Creating API key...")
    key = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "eval-key", "role": "admin"},
    )["data"]["raw_key"]
    print(f"   API Key: {key[:20]}...")

    print("\n3. Creating the agent (its tool points at an unroutable host)...")
    agent_id = api("POST", "/v1/agents", AGENT, token=key)["data"]["id"]
    api("POST", f"/v1/agents/{agent_id}/publish", {}, token=key)
    print(f"   Agent: {agent_id}")
    print(f"   book_table -> {NEVER_CALLED}")

    print("\n4. Storing three scenarios (same turns, different mocks)...")
    target = {"type": "agent", "agent_id": agent_id}
    scenario_ids = {}
    for scenario in SCENARIOS:
        created = api(
            "POST", "/v1/eval-scenarios", {**scenario, "default_target": target}, key
        )["data"]
        scenario_ids[scenario["name"]] = created["id"]
        warnings = created.get("warnings") or []
        note = f"  ({warnings[0]['code']})" if warnings else ""
        print(f"   {scenario['name']}: {created['id']}{note}")

    print("\n5. Running them...")
    results = {}
    for name, scenario_id in scenario_ids.items():
        queued = api(
            "POST",
            "/v1/eval-runs",
            {"scenario_id": scenario_id, "target": target, "iterations": 1},
            key,
        )["data"]
        run = wait_for(queued["runs"][0]["id"], key, args.timeout)
        results[name] = run
        detail = f" — {run['error']}" if run["error"] else ""
        print(
            f"   {name}: {run['status']} "
            f"({run['passed_count']}/{run['iterations']}){detail}"
        )

    print("\n" + "=" * 60)
    print("  What just happened")
    print("=" * 60)
    print(
        "\n  booking-confirmed passed while book_table pointed at an unroutable\n"
        "  host, which is the proof: the mock short-circuits the dispatch before\n"
        "  the webhook, so nothing was reserved anywhere.\n"
        "\n  booking-refused is the same conversation with the other mock. A tool\n"
        "  that can fail needs a test for the failing branch, because 'claims a\n"
        "  table it does not have' is the expensive bug.\n"
        "\n  no-mock-at-all is `errored`, not `failed`: nobody learned anything\n"
        "  about the agent, and `mock_only` refused the call rather than letting\n"
        "  an eval book a real table. `tool_policy: live` is the typed opt-in for\n"
        "  tools that only read.\n"
    )
    print(f"  Inspect a run: GET {BASE_URL}/v1/eval-runs/<id>")
    print(f"  API key: {key}\n")


if __name__ == "__main__":
    main()
