"""Receptionist Example — Complete Setup Script

Run this after starting the server to create a working receptionist
that answers calls on your Twilio number.

Prerequisites:
  1. Server running: `make docker-up && make run`
  2. .env configured with TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY, CARTESIA_API_KEY
  3. A Twilio phone number (get the SID from Twilio Console)

Usage:
  python examples/receptionist/setup.py \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \\
    --server-url "https://your-ngrok-url.ngrok.io"

Then call your Twilio number and talk to the receptionist!
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
        else:
            resp = client.get(path, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the receptionist example")
    parser.add_argument(
        "--twilio-number",
        required=True,
        help="Your Twilio phone number in E.164 format (e.g., +15559876543)",
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
        "--transfer-number",
        default="+15551234567",
        help="Phone number to transfer appointment calls to",
    )
    parser.add_argument(
        "--cartesia-voice",
        default="f9836c6e-a0bd-460e-9d3c-f7299fa60f94",
        help="Cartesia voice ID",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall Receptionist Example — Setup")
    print("=" * 60)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "receptionist-demo"})
    project_id = result["data"]["id"]
    print(f"   Project: {project_id}")

    # --- Step 2: Create API key ---
    print("\n2. Creating API key...")
    result = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "setup-key", "role": "admin"},
    )
    api_key = result["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    # --- Step 3: Create billing assistant (handoff target) ---
    print("\n3. Creating billing assistant...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "billing-specialist",
            "config": {
                "system_prompt": (
                    "You are a billing specialist. Help callers with:\n"
                    "- Invoice questions\n"
                    "- Payment status\n"
                    "- Insurance claims\n"
                    "Be precise with numbers. If you cannot help, apologize and "
                    "offer to transfer back to the receptionist."
                ),
                "first_message": (
                    "You've been transferred to billing. How can I help you?"
                ),
                "stt": {
                    "provider": "cartesia",
                    "model": "ink-whisper",
                    "language": "en",
                },
                "llm": {"provider": "openai", "model": "gpt-4o"},
                "tts": {
                    "provider": "cartesia",
                    "model": "sonic-3.5",
                    "voice": "f9836c6e-a0bd-460e-9d3c-f7299fa60f94",
                    "extra": {
                        "emotion": ["positivity:high", "relaxation:highest"],
                        "language": "en"
                },
                },
            },
        },
        token=api_key,
    )
    billing_id = result["data"]["id"]
    print(f"   Billing Agent: {billing_id}")

    # Publish it
    api("POST", f"/v1/agents/{billing_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 4: Create receptionist assistant ---
    print("\n4. Creating receptionist agent...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "receptionist",
            "config": {
                "system_prompt": (
                    "You are a friendly receptionist for a dental clinic.\n\n"
                    "Your job is to understand why the caller is calling and route them:\n\n"
                    "1. **Appointments** (schedule, reschedule, cancel) "
                    f"→ Transfer to front desk at {args.transfer_number}\n"
                    f"2. **Billing or insurance** → Hand off to billing assistant "
                    f"(ID: {billing_id})\n"
                    "3. **Emergency** → Tell them to call 911\n"
                    "4. **General questions** → Answer yourself:\n"
                    "   - Hours: Mon-Fri 9am-5pm\n"
                    "   - Address: 123 Main Street\n"
                    "   - Dr. Smith and Dr. Johnson are available\n\n"
                    "Always greet warmly. Ask clarifying questions if the intent "
                    "is unclear. Confirm before transferring."
                ),
                "first_message": (
                    "Thank you for calling the dental clinic! "
                    "How can I help you today?"
                ),
                "tools": [
                    {
                        "name": "transfer_call",
                        "description": "Transfer the caller to a human agent.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "target_number": {
                                    "type": "string",
                                    "description": "Phone number in E.164 format",
                                },
                                "transfer_mode": {
                                    "type": "string",
                                    "enum": ["warm", "cold"],
                                    "description": "Transfer type",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Why transferring",
                                },
                            },
                            "required": ["target_number"],
                        },
                    },
                    {
                        "name": "handoff_to_agent",
                        "description": "Hand off to another AI assistant.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "agent_id": {
                                    "type": "string",
                                    "description": "Target assistant ID",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Why handing off",
                                },
                            },
                            "required": ["agent_id"],
                        },
                    },
                    {
                        "name": "end_call",
                        "description": "End the call after conversation is complete.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"},
                            },
                        },
                    },
                ],
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
                    "extra": {"emotion": ["positivity:high", "curiosity"]},
                },
                "silence_timeout_ms": 1200,
                "interruption_enabled": True,
                "smart_turn_detection": True,
                "smart_turn_stop_secs": 3.0,
                "max_call_duration_seconds": 600,
            },
        },
        token=api_key,
    )
    receptionist_id = result["data"]["id"]
    print(f"   Receptionist: {receptionist_id}")

    # Publish it
    api("POST", f"/v1/agents/{receptionist_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 5: Bind phone number ---
    print("\n5. Binding Twilio phone number...")
    result = api(
        "POST",
        "/v1/phone-numbers",
        {
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": receptionist_id,
        },
        token=api_key,
    )
    print(f"   Bound: {args.twilio_number} → receptionist")

    # --- Step 6: Configure Twilio webhooks ---
    print("\n6. Configuring Twilio webhooks...")
    voice_url = f"{args.server_url}/webhooks/twilio/voice/inbound"
    status_url = f"{args.server_url}/webhooks/twilio/status"

    import os

    from twilio.rest import Client

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")

    if twilio_sid and twilio_token:
        client = Client(twilio_sid, twilio_token)
        client.incoming_phone_numbers(args.twilio_number_sid).update(
            voice_url=voice_url,
            voice_method="POST",
            status_callback=status_url,
            status_callback_method="POST",
        )
        print(f"   Voice URL: {voice_url}")
        print(f"   Status URL: {status_url}")
    else:
        print("   TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set — configure manually:")
        print(f"   Voice URL:  {voice_url}")
        print(f"   Status URL: {status_url}")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Call {args.twilio_number} to talk to the receptionist.")
    print("\n  Try saying:")
    print('    "I need to schedule an appointment"  → transfers to human')
    print('    "I have a question about my bill"    → hands off to billing AI')
    print('    "What are your hours?"               → answers directly')
    print("\n  Monitor calls:")
    print(f"    curl {BASE_URL}/v1/calls -H 'Authorization: Bearer {api_key[:20]}...'")
    print(f"\n  API Key (save this): {api_key}")
    print()


if __name__ == "__main__":
    main()
