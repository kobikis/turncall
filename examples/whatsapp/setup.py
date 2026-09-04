"""WhatsApp Example — Complete Setup Script

Run this after starting the server to create a working WhatsApp assistant.

Prerequisites:
  1. Server running: `make docker-up && make migrate && make run`
  2. .env configured with WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
     WHATSAPP_APP_SECRET, WHATSAPP_WEBHOOK_VERIFY_TOKEN, OPENAI_API_KEY
  3. ngrok running and WhatsApp webhook configured (see README.md)

Usage:
  python examples/whatsapp/setup.py \\
    --whatsapp-number "+15559876543" \\
    --whatsapp-phone-number-id "123456789"

Then message your WhatsApp Business number!
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
    parser = argparse.ArgumentParser(description="Set up the WhatsApp example")
    parser.add_argument(
        "--whatsapp-number",
        required=True,
        help="Your WhatsApp Business number in E.164 format (e.g., +15559876543)",
    )
    parser.add_argument(
        "--whatsapp-phone-number-id",
        required=True,
        help="WhatsApp Phone Number ID from Meta Developer Console",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall WhatsApp Example — Setup")
    print("=" * 60)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "whatsapp-demo"})
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
    print("\n3. Creating WhatsApp assistant...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "whatsapp-support",
            "config": {
                "system_prompt": (
                    "You are a helpful customer support assistant for Acme Corp.\n\n"
                    "You handle inquiries via WhatsApp messages and voice calls.\n\n"
                    "For text messages: Keep replies concise and friendly. Use "
                    "short paragraphs. Avoid long lists.\n\n"
                    "For voice calls: Speak naturally and conversationally.\n\n"
                    "You can help with:\n"
                    "- Business hours: Mon-Fri 9am-6pm, Sat 10am-2pm\n"
                    "- Store location: 456 Oak Avenue, Suite 100\n"
                    "- Return policy: 30-day returns with receipt\n"
                    "- Order status: Ask for order number, then look it up\n"
                    "- General product questions\n\n"
                    "If you cannot help, suggest they visit our website.\n"
                    "Be friendly and professional."
                ),
                "first_message": (
                    "Hi! Thanks for reaching out to Acme Corp. "
                    "How can I help you today?"
                ),
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

    # --- Step 4: Bind phone number with WhatsApp enabled ---
    print("\n4. Binding WhatsApp Business number...")
    result = api(
        "POST",
        "/v1/phone-numbers",
        {
            "external_number_sid": f"WA:{args.whatsapp_phone_number_id}",
            "e164_number": args.whatsapp_number,
            "routing_target_type": "agent",
            "routing_target_id": assistant_id,
            "whatsapp_enabled": True,
        },
        token=api_key,
    )
    wa_status = "enabled" if result["data"].get("whatsapp_enabled") else "disabled"
    print(
        f"   Bound: {args.whatsapp_number} -> whatsapp-support (WhatsApp: {wa_status})"
    )

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Message {args.whatsapp_number} on WhatsApp to chat with the assistant.")
    print(f"  Call {args.whatsapp_number} on WhatsApp for voice conversation.")
    print("\n  Try messaging:")
    print('    "What are your hours?"        -> Business hours')
    print('    "Where are you located?"      -> Store address')
    print('    "I want to return something"  -> Return policy')
    print("\n  Use the Chat API (same assistant):")
    print(f"    curl -X POST {BASE_URL}/v1/chat \\")
    print(f'      -H "Authorization: Bearer {api_key[:20]}..." \\')
    print('      -H "Content-Type: application/json" \\')
    print(
        f'      -d \'{{"assistant_id": "{assistant_id}", '
        '"message": "What are your hours?"}}\''
    )
    print(f"\n  API Key (save this): {api_key}")
    print()


if __name__ == "__main__":
    main()
