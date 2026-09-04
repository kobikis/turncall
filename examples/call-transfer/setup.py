#!/usr/bin/env python3
"""Call transfer example.

Creates an agent that transfers to a human (cold or warm with a briefing),
binds your Twilio number, and prints ready-to-run manual-transfer commands.

Usage:
    # 1. Start turncall: make run
    # 2. Run this script:
    python examples/call-transfer/setup.py \\
        --twilio-number-sid PN_YOUR_SID \\
        --twilio-number +15551234567 \\
        --transfer-to +15557654321

    # Then call your Twilio number and ask for a human, or use the
    # manual REST commands the script prints.

Design: adr/0009-call-transfer-warm-cold.md. Twilio PSTN only.

Requirements:
    pip install httpx
"""

import argparse
import os

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")

BASE_URL = "http://localhost:8090"


def main() -> None:
    parser = argparse.ArgumentParser(description="Call transfer setup")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--project-name", default="Transfer Demo")
    parser.add_argument(
        "--twilio-number-sid", required=True, help="Twilio Phone Number SID (PN...)"
    )
    parser.add_argument(
        "--twilio-number", required=True, help="Your Twilio number, E.164 (+1555...)"
    )
    parser.add_argument(
        "--transfer-to",
        required=True,
        help="Operator/human number to transfer to, E.164 (+1555...)",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    if not PLATFORM_KEY:
        raise SystemExit(
            "PLATFORM_API_KEY is not set — add it to .env (see env.example)"
        )
    client = httpx.Client(
        base_url=base, timeout=30.0, headers={"X-Platform-Key": PLATFORM_KEY}
    )

    # 1. Project
    print("Creating project...")
    resp = client.post("/v1/projects", json={"name": args.project_name})
    resp.raise_for_status()
    project_id = resp.json()["data"]["id"]
    print(f"  Project: {project_id}")

    # 2. API key
    resp = client.post(
        "/v1/api-keys",
        params={"project_id": project_id},
        json={"name": "transfer-demo", "role": "admin"},
    )
    resp.raise_for_status()
    api_key = resp.json()["data"]["raw_key"]
    print(f"  API Key: {api_key}")
    headers = {"Authorization": f"Bearer {api_key}"}

    # 3. Agent that knows when (and how) to transfer.
    print("\nCreating transfer-savvy agent...")
    resp = client.post(
        "/v1/agents",
        json={
            "name": "front-desk",
            "config": {
                "system_prompt": (
                    "You are the front desk for TechCorp. Handle simple questions "
                    "yourself. If the caller asks for a human, or wants billing or "
                    "an escalation, transfer them.\n"
                    f"Use the transfer_call tool with target_number {args.transfer_to}. "
                    "For billing or upset callers use transfer_mode 'warm' and pass a "
                    "one-line briefing summarizing who they are and why. Otherwise "
                    "transfer_mode 'cold' is fine. Always set a transfer_message so the "
                    "caller knows they're being connected, and a fallback_message in "
                    "case no one answers."
                ),
                "first_message": "Thanks for calling TechCorp, how can I help?",
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
            },
        },
        headers=headers,
    )
    resp.raise_for_status()
    agent = resp.json()["data"]
    agent_id = agent["id"]
    print(f"  Agent: {agent_id} (v{agent['version']})")

    resp = client.post(f"/v1/agents/{agent_id}/publish", headers=headers)
    resp.raise_for_status()
    print(f"  Published: v{resp.json()['data']['version']}")

    # 4. Bind phone number
    resp = client.post(
        "/v1/phone-numbers",
        json={
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
        },
        headers=headers,
    )
    resp.raise_for_status()
    print(f"  Phone: {args.twilio_number} -> agent {agent_id}")

    # 5. Print manual-transfer recipes
    to = args.transfer_to
    print("\n--- Try it ---")
    print(f"Call {args.twilio_number} and ask for a human — the agent transfers.")
    print("\nOr transfer a live call manually (CALL_ID from the call.started event):\n")

    print("# Cold transfer")
    print(
        f"curl -X POST {base}/v1/calls/CALL_ID/transfer \\\n"
        f'  -H "Authorization: Bearer {api_key}" -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"target_number": "{to}", "transfer_mode": "cold"}}\'\n'
    )

    print("# Warm transfer with a static briefing to the operator")
    print(
        f"curl -X POST {base}/v1/calls/CALL_ID/transfer \\\n"
        f'  -H "Authorization: Bearer {api_key}" -H "Content-Type: application/json" \\\n'
        "  -d '{\n"
        f'        "target_number": "{to}",\n'
        '        "transfer_mode": "warm",\n'
        '        "transfer_message": "Please hold while I bring in a colleague.",\n'
        '        "briefing": "Premium caller disputing a double charge on order #4471.",\n'
        '        "fallback_message": "Sorry, no one is available right now."\n'
        "      }'\n"
    )

    print("# Warm transfer with an auto-generated summary briefing")
    print(
        f"curl -X POST {base}/v1/calls/CALL_ID/transfer \\\n"
        f'  -H "Authorization: Bearer {api_key}" -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"target_number": "{to}", "transfer_mode": "warm", '
        '"briefing": {"from_summary": true}}\'\n'
    )

    print("Watch events with: python examples/events-webhook/setup.py "
          f"--api-key {api_key} --events call.transferred transfer.answered call.ended")


if __name__ == "__main__":
    main()
