"""A/B Testing Example — Agent Versioning + Weighted Routing

Demonstrates TurnCall's agent versioning and A/B testing:
  1. Creates an agent v1 (concise style) and publishes it
  2. Creates v2 (friendly style) and publishes it (auto-archives v1)
  3. Rolls back to v1 to show rollback
  4. Re-publishes v2 and sets up 50/50 A/B test
  5. Shows how to monitor and conclude the test

Prerequisites:
  1. Server running: `make docker-up && make run`
  2. .env configured with TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, CARTESIA_API_KEY
  3. A Twilio phone number

Usage:
  python examples/ab-testing/setup.py \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \\
    --server-url "https://your-ngrok-url.ngrok.io"

Then call your number multiple times — you'll randomly get v1 or v2!
"""

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")

BASE_URL = "http://localhost:8090"


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

    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        if method == "POST":
            resp = client.post(path, json=data, headers=headers)
        elif method == "PUT":
            resp = client.put(path, json=data, headers=headers)
        elif method == "DELETE":
            resp = client.delete(path, headers=headers)
        else:
            resp = client.get(path, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the A/B testing example")
    parser.add_argument(
        "--twilio-number",
        required=True,
        help="Your Twilio phone number in E.164 format",
    )
    parser.add_argument(
        "--twilio-number-sid",
        required=True,
        help="Twilio Phone Number SID (starts with PN)",
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Public URL where TurnCall is reachable (e.g., https://abc123.ngrok.io)",
    )
    parser.add_argument(
        "--cartesia-voice",
        default="f9836c6e-a0bd-460e-9d3c-f7299fa60f94",
        help="Cartesia voice ID",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall A/B Testing Example")
    print("=" * 60)

    # --- Step 1: Create project + API key ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "ab-testing-demo"})
    project_id = result["data"]["id"]
    print(f"   Project: {project_id}")

    print("\n2. Creating API key...")
    result = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "ab-key", "role": "admin"},
    )
    api_key = result["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    # --- Step 2: Create agent v1 — concise style ---
    print("\n3. Creating agent v1 (concise style)...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "support-agent",
            "config": {
                "system_prompt": (
                    "You are a support agent. Be concise and direct.\n"
                    "Answer questions in 1-2 sentences maximum.\n"
                    "Don't use filler words. Get to the point quickly.\n\n"
                    "Company: TechCorp\n"
                    "Hours: Mon-Fri 9am-5pm\n"
                    "Support email: help@techcorp.com"
                ),
                "first_message": "TechCorp support. How can I help?",
                "stt": {
                    "provider": "cartesia",
                    "model": "ink-whisper",
                    "language": "en",
                },
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "tts": {
                    "provider": "cartesia",
                    "model": "sonic-3.5",
                    "voice": args.cartesia_voice,
                    "extra": {"emotion": ["confidence:high"]},
                },
                "tools": [
                    {
                        "name": "end_call",
                        "description": "End the call when the conversation is complete.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"},
                            },
                        },
                    },
                ],
            },
        },
        token=api_key,
    )
    v1_id = result["data"]["id"]
    v1_version = result["data"]["version"]
    print(f"   Agent v{v1_version}: {v1_id}")

    # Publish v1
    api("POST", f"/v1/agents/{v1_id}/publish", {}, token=api_key)
    print("   Published v1 (concise style)")

    # --- Step 3: Bind phone number to v1 ---
    print("\n4. Binding phone number to v1...")
    result = api(
        "POST",
        "/v1/phone-numbers",
        {
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": v1_id,
        },
        token=api_key,
    )
    phone_number_id = result["data"]["id"]
    print(f"   Bound: {args.twilio_number} -> v1")

    # --- Step 4: Configure Twilio webhooks ---
    print("\n5. Configuring Twilio webhooks...")
    import os

    from twilio.rest import Client

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")

    voice_url = f"{args.server_url}/webhooks/twilio/voice/inbound"
    status_url = f"{args.server_url}/webhooks/twilio/status"

    if twilio_sid and twilio_token:
        client = Client(twilio_sid, twilio_token)
        client.incoming_phone_numbers(args.twilio_number_sid).update(
            voice_url=voice_url,
            voice_method="POST",
            status_callback=status_url,
            status_callback_method="POST",
        )
        print(f"   Voice URL: {voice_url}")
    else:
        print("   TWILIO creds not set — configure webhooks manually:")
        print(f"   Voice URL:  {voice_url}")
        print(f"   Status URL: {status_url}")

    # --- Step 5: Create agent v2 — friendly style ---
    print("\n6. Creating agent v2 (friendly style)...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "support-agent",
            "config": {
                "system_prompt": (
                    "You are a warm and friendly support agent for TechCorp! 😊\n"
                    "Be enthusiastic and empathetic. Use the caller's name if they "
                    "share it. Ask follow-up questions to make sure they're satisfied.\n"
                    "Always end with 'Is there anything else I can help with?'\n\n"
                    "Company: TechCorp\n"
                    "Hours: Mon-Fri 9am-5pm\n"
                    "Support email: help@techcorp.com"
                ),
                "first_message": (
                    "Hey there! Welcome to TechCorp support! "
                    "I'm so glad you called. What can I help you with today?"
                ),
                "stt": {
                    "provider": "cartesia",
                    "model": "ink-whisper",
                    "language": "en",
                },
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "tts": {
                    "provider": "cartesia",
                    "model": "sonic-3.5",
                    "voice": args.cartesia_voice,
                    "extra": {"emotion": ["positivity:high", "excitement"]},
                },
                "tools": [
                    {
                        "name": "end_call",
                        "description": "End the call when the conversation is complete.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"},
                            },
                        },
                    },
                ],
            },
        },
        token=api_key,
    )
    v2_id = result["data"]["id"]
    v2_version = result["data"]["version"]
    print(f"   Agent v{v2_version}: {v2_id}")

    # Publish v2 — auto-archives v1, auto-promotes phone number
    api("POST", f"/v1/agents/{v2_id}/publish", {}, token=api_key)
    print("   Published v2 (friendly style)")
    print("   -> v1 auto-archived, phone number auto-promoted to v2")

    # --- Step 6: Show version history ---
    print("\n7. Version history:")
    result = api("GET", f"/v1/agents/{v2_id}/versions", token=api_key)
    for v in result["data"]:
        print(f"   v{v['version']} ({v['state']}) — {v['id']}")

    # --- Step 7: Demonstrate rollback ---
    print("\n8. Rolling back to v1...")
    api("POST", f"/v1/agents/{v1_id}/rollback", {}, token=api_key)
    print("   -> v1 restored to published, v2 archived, phone number updated")

    # Re-publish v2 for A/B test
    print("\n9. Re-publishing v2 for A/B test...")
    # Unarchive v2 manually by rolling back to v2 (which archives v1)
    api("POST", f"/v1/agents/{v2_id}/rollback", {}, token=api_key)
    # Now v2 is published, v1 is archived again

    # --- Step 8: Set up A/B test ---
    print("\n10. Setting up 50/50 A/B test...")
    # First, un-archive v1 for A/B test (rollback, then we'll use weights)
    api("POST", f"/v1/agents/{v1_id}/rollback", {}, token=api_key)
    # Now v1 is published, v2 is archived — but we want both available
    # Re-publish v2 by creating it fresh... actually let's use weighted routing directly
    # Both agents exist, weights reference by ID regardless of state

    api(
        "PUT",
        f"/v1/phone-numbers/{phone_number_id}/routing",
        {
            "weights": [
                {"agent_id": v1_id, "weight": 50},
                {"agent_id": v2_id, "weight": 50},
            ]
        },
        token=api_key,
    )
    print("   A/B test active: 50% v1 (concise) / 50% v2 (friendly)")

    # --- Step 9: Show routing config ---
    print("\n11. Current routing config:")
    result = api("GET", f"/v1/phone-numbers/{phone_number_id}/routing", token=api_key)
    routing = result["data"]
    print(f"   Mode: {routing['mode']}")
    if routing.get("weights"):
        for w in routing["weights"]:
            print(f"   -> {w['agent_id']}: {w['weight']}%")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  A/B TEST ACTIVE!")
    print("=" * 60)
    print(f"\n  Call {args.twilio_number} multiple times.")
    print("  ~50% of calls will get the concise agent (v1)")
    print("  ~50% of calls will get the friendly agent (v2)")
    print("  (Same caller always gets the same variant)")
    print("\n  To conclude the test (pick a winner):")
    print("    # Clear weights, revert to single agent:")
    print(
        f"    curl -X DELETE {BASE_URL}/v1/phone-numbers/{phone_number_id}/routing \\"
    )
    print(f"      -H 'Authorization: Bearer {api_key[:20]}...'")
    print("\n  To change split (e.g., 80/20):")
    weights_json = (
        '{"weights": ['
        f'{{"agent_id": "{v1_id}", "weight": 80}}, '
        f'{{"agent_id": "{v2_id}", "weight": 20}}'
        "]}"
    )
    print(f"    curl -X PUT {BASE_URL}/v1/phone-numbers/{phone_number_id}/routing \\")
    print(f"      -H 'Authorization: Bearer {api_key[:20]}...' \\")
    print(f"      -d '{weights_json}'")
    print("\n  Monitor calls:")
    print(f"    curl {BASE_URL}/v1/calls -H 'Authorization: Bearer {api_key[:20]}...'")
    print(f"\n  API Key (save this): {api_key}")
    print()


if __name__ == "__main__":
    main()
