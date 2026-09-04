#!/usr/bin/env python3
"""Events Webhook — full setup + subscribe to TurnCall events.

Creates project, agent (with analysis), binds phone number,
and subscribes to webhook events. Run webhook_server.py first.

Usage:
    # Terminal 1: Start the webhook server
    uvicorn webhook_server:app --port 9001

    # Terminal 2: Full setup (project + agent + phone + webhook)
    python setup.py \\
        --twilio-number-sid PN_YOUR_SID \\
        --twilio-number +15551234567

    # Or use an existing API key (skip project/agent/phone creation)
    python setup.py --api-key tc_YOUR_KEY

    # Subscribe to specific events only
    python setup.py --api-key tc_YOUR_KEY --events call.ended transcript.final
"""

import argparse
import os

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")


def _setup_project(
    client: httpx.Client,
    project_name: str,
    twilio_number_sid: str,
    twilio_number: str,
) -> str:
    """Create project, API key, agent with analysis, and bind phone number."""
    # Project
    resp = client.post("/v1/projects", json={"name": project_name})
    resp.raise_for_status()
    project_id = resp.json()["data"]["id"]
    print(f"  Project: {project_id}")

    # API key
    resp = client.post(
        "/v1/api-keys",
        params={"project_id": project_id},
        json={"name": "events-webhook", "role": "admin"},
    )
    resp.raise_for_status()
    raw_key = resp.json()["data"]["raw_key"]
    print(f"  API Key: {raw_key}")

    headers = {"Authorization": f"Bearer {raw_key}"}

    # Agent with analysis
    resp = client.post(
        "/v1/agents",
        json={
            "name": "events-demo-agent",
            "config": {
                "system_prompt": (
                    "You are a helpful support agent. " "Be professional and concise."
                ),
                "first_message": "Hello! How can I help you today?",
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "analysis": {
                    "enabled": True,
                    "summary_enabled": True,
                    "success_evaluation": {
                        "enabled": True,
                        "scale": "pass_fail",
                    },
                    "sentiment_enabled": True,
                },
            },
        },
        headers=headers,
    )
    resp.raise_for_status()
    agent = resp.json()["data"]
    agent_id = agent["id"]
    print(f"  Agent: {agent_id} (v{agent['version']})")

    # Publish
    resp = client.post(f"/v1/agents/{agent_id}/publish", headers=headers)
    resp.raise_for_status()
    print(f"  Published: v{resp.json()['data']['version']}")

    # Bind phone number
    resp = client.post(
        "/v1/phone-numbers",
        json={
            "external_number_sid": twilio_number_sid,
            "e164_number": twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
        },
        headers=headers,
    )
    resp.raise_for_status()
    print(f"  Phone: {twilio_number} -> agent {agent_id}")

    return raw_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Subscribe to TurnCall events")
    parser.add_argument("--base-url", default="http://localhost:8090")
    parser.add_argument(
        "--webhook-url",
        default="http://host.docker.internal:9001/events",
        help="URL of your webhook server (host.docker.internal reaches the host from the Dockerized API; use localhost if running the API on the host)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="TurnCall API key (tc_...). If omitted, creates project + agent + phone.",
    )
    parser.add_argument("--project-name", default="Events Demo")
    parser.add_argument(
        "--twilio-number-sid",
        default=None,
        help="Twilio Phone Number SID (PN...). Required if no --api-key.",
    )
    parser.add_argument(
        "--twilio-number",
        default=None,
        help="Twilio number in E.164 (+15551234567). Required if no --api-key.",
    )
    parser.add_argument(
        "--events",
        nargs="*",
        default=["*"],
        help='Events to subscribe to (default: "*" for all)',
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

    # Get or create API key
    api_key = args.api_key
    if not api_key:
        if not args.twilio_number_sid or not args.twilio_number:
            parser.error(
                "--twilio-number-sid and --twilio-number are required "
                "when --api-key is not provided"
            )
        print("Creating project + agent + phone number...")
        api_key = _setup_project(
            client,
            args.project_name,
            args.twilio_number_sid,
            args.twilio_number,
        )

    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"Subscribing to events: {args.events}")
    print(f"Webhook URL: {args.webhook_url}")

    resp = client.post(
        "/v1/webhooks",
        json={
            "url": args.webhook_url,
            "events": args.events,
        },
        headers=headers,
    )
    resp.raise_for_status()
    webhook = resp.json()["data"]

    print(f"\nSubscription created: {webhook['id']}")
    print(f"Events: {args.events}")
    print("\nNow make a call — events will appear in the webhook server terminal.")

    print("\n--- Available Events ---")
    events = [
        ("call.initializing", "Pre-call init (webhook routing)"),
        ("call.started", "Pipeline starts, call in progress"),
        ("call.ended", "Post-call: transcript + recording + analysis"),
        ("call.failed", "Call errored"),
        ("call.transferred", "Transfer initiated"),
        ("call.agent_handoff", "Handed off to another agent"),
        ("transcript.partial", "Interim STT result"),
        ("transcript.final", "Final STT utterance"),
        ("tool.called", "Tool invoked by LLM"),
        ("tool.result", "Tool execution finished"),
        ("recording.ready", "Recording stored to local/S3"),
        ("session.created", "New SMS/chat session"),
        ("session.updated", "Session activity"),
        ("session.deleted", "Session expired"),
        ("chat.created", "New chat message"),
        ("context.injected", "Context message injected"),
        ("dtmf.sent", "DTMF tones sent"),
        ("error.raised", "Runtime error"),
    ]
    for name, desc in events:
        print(f"  {name:<25} {desc}")


if __name__ == "__main__":
    main()
