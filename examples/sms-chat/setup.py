"""SMS Chat Example — Complete Setup Script

Run this after starting the server to create a working SMS chatbot
on your Twilio number.

Prerequisites:
  1. Server running: `make docker-up && make migrate && make run`
  2. .env configured with TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY
  3. A 10DLC-approved Twilio phone number

Usage:
  python examples/sms-chat/setup.py \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \\
    --server-url "https://your-ngrok-url.ngrok.io"

Then text your Twilio number!
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
    parser = argparse.ArgumentParser(description="Set up the SMS chat example")
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
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall SMS Chat Example — Setup")
    print("=" * 60)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "sms-chat-demo"})
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

    # --- Step 3: Create assistant ---
    print("\n3. Creating support assistant...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "sms-support",
            "config": {
                "system_prompt": (
                    "You are a helpful customer support assistant for Acme Corp.\n\n"
                    "You handle inquiries via SMS text messages. Keep replies "
                    "concise (under 320 characters when possible) since responses "
                    "are delivered as SMS.\n\n"
                    "You can help with:\n"
                    "- Business hours: Mon-Fri 9am-6pm, Sat 10am-2pm\n"
                    "- Store location: 456 Oak Avenue, Suite 100\n"
                    "- Return policy: 30-day returns with receipt\n"
                    "- Order status: Ask for order number, then look it up\n"
                    "- General product questions\n\n"
                    "If you cannot help, suggest they call during business hours.\n"
                    "Be friendly but brief. Use line breaks sparingly."
                ),
                "first_message": ("Hi! Thanks for texting Acme Corp. How can I help?"),
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "temperature": 0.7,
                    "max_tokens": 256,
                },
                "stt": {"provider": "deepgram", "model": "nova-3-general"},
                "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
            },
        },
        token=api_key,
    )
    assistant_id = result["data"]["id"]
    print(f"   Agent: {assistant_id}")

    # Publish it
    api("POST", f"/v1/agents/{assistant_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 4: Bind phone number with SMS enabled ---
    print("\n4. Binding Twilio phone number (voice + SMS)...")
    result = api(
        "POST",
        "/v1/phone-numbers",
        {
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": assistant_id,
            "sms_enabled": True,
        },
        token=api_key,
    )
    sms_status = "enabled" if result["data"]["sms_enabled"] else "disabled"
    print(f"   Bound: {args.twilio_number} → sms-support (SMS: {sms_status})")

    # --- Step 5: Configure Twilio webhooks ---
    print("\n5. Configuring Twilio webhooks...")
    voice_url = f"{args.server_url}/webhooks/twilio/voice/inbound"
    sms_url = f"{args.server_url}/webhooks/twilio/sms/inbound"
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
            sms_url=sms_url,
            sms_method="POST",
        )
        print(f"   Voice URL: {voice_url}")
        print(f"   SMS URL:   {sms_url}")
        print(f"   Status URL: {status_url}")
    else:
        print("   TWILIO credentials not set — configure manually:")
        print(f"   Voice URL:  {voice_url}")
        print(f"   SMS URL:    {sms_url}")
        print(f"   Status URL: {status_url}")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Text {args.twilio_number} to chat with the assistant.")
    print(f"  Call {args.twilio_number} to talk by voice.")
    print("\n  Try texting:")
    print('    "What are your hours?"        → Business hours')
    print('    "Where are you located?"      → Store address')
    print('    "I want to return something"  → Return policy')
    print("\n  Use the Chat API:")
    print(f"    curl -X POST {BASE_URL}/v1/chat \\")
    print(f'      -H "Authorization: Bearer {api_key[:20]}..." \\')
    print('      -H "Content-Type: application/json" \\')
    print(
        f'      -d \'{{"assistant_id": "{assistant_id}", '
        '"message": "What are your hours?"}}\''
    )
    print("\n  List sessions:")
    print(f"    curl {BASE_URL}/v1/chat/sessions \\")
    print(f'      -H "Authorization: Bearer {api_key[:20]}..."')
    print(f"\n  API Key (save this): {api_key}")
    print()


if __name__ == "__main__":
    main()
