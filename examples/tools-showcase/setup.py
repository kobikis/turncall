"""Tools Showcase — Setup Script

Creates a project with a support agent that demonstrates ALL tool types:
  - Built-in: end_call, transfer_call, handoff_to_agent, send_dtmf
  - Webhook: lookup_customer, check_order_status, create_ticket
  - Pre-call: call-init with knowledge_context + template variables

Prerequisites:
  1. TurnCall server running: `make docker-up && make run`
  2. Webhook server running: `uvicorn webhook_server:app --port 9000`
  3. Both exposed via ngrok

Usage:
  python examples/tools-showcase/setup.py \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxx" \\
    --turncall-url "https://xxxx.ngrok.io" \\
    --webhook-url "https://yyyy.ngrok.io"
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
        else:
            resp = client.get(path, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the tools showcase example")
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
        "--turncall-url",
        required=True,
        help="Public URL where TurnCall is reachable (ngrok URL)",
    )
    parser.add_argument(
        "--webhook-url",
        required=True,
        help="Public URL where the webhook_server.py is reachable (ngrok URL)",
    )
    parser.add_argument(
        "--transfer-number",
        default="+15551234567",
        help="Phone number for transfer_call demo",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall Tools Showcase — Setup")
    print("=" * 60)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "tools-showcase"})
    project_id = result["data"]["id"]
    print(f"   Project: {project_id}")

    # --- Step 2: Create API key ---
    print("\n2. Creating API key...")
    result = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "tools-key", "role": "admin"},
    )
    api_key = result["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    # --- Step 3: Create billing assistant (handoff target) ---
    print("\n3. Creating billing assistant (handoff target)...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "billing-specialist",
            "config": {
                "system_prompt": (
                    "You are a billing specialist. Help callers with invoices, "
                    "payments, refunds, and insurance claims.\n\n"
                    "Be precise with numbers. If you cannot resolve the issue, "
                    "offer to create a ticket."
                ),
                "first_message": "You've been connected to billing. How can I help?",
                "stt": {"provider": "deepgram", "model": "nova-3-general", "language": "en"},
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
                "tools": [
                    {
                        "name": "end_call",
                        "description": (
                            "End the call when the billing issue is resolved "
                            "or the customer says goodbye."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "enum": [
                                        "resolved",
                                        "customer_requested",
                                        "escalation_needed",
                                    ],
                                },
                            },
                        },
                    },
                    {
                        "name": "create_ticket",
                        "description": (
                            "Create a support ticket when the billing issue "
                            "cannot be resolved immediately."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "subject": {
                                    "type": "string",
                                    "description": "Brief ticket subject",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Detailed issue description",
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high", "urgent"],
                                },
                            },
                            "required": ["subject", "description"],
                        },
                        "webhook_url": f"{args.webhook_url}/tools/create-ticket",
                        "timeout_seconds": 10,
                    },
                ],
            },
        },
        token=api_key,
    )
    billing_id = result["data"]["id"]
    print(f"   Billing Agent: {billing_id}")
    api("POST", f"/v1/agents/{billing_id}/publish", {}, token=api_key)

    # --- Step 4: Create main support agent ---
    print("\n4. Creating support agent (all tool types)...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "support-agent",
            "config": {
                "system_prompt": (
                    "You are a support agent for Acme Corp.\n\n"
                    "The caller is {{customer_name}} (Account: {{account_id}}, "
                    "Tier: {{tier}}).\n\n"
                    "Your capabilities:\n"
                    "1. Look up customer info (lookup_customer)\n"
                    "2. Check order status (check_order_status)\n"
                    "3. Create support tickets (create_ticket)\n"
                    "4. Transfer to a human agent (transfer_call)\n"
                    "5. Hand off to billing specialist (handoff_to_agent)\n"
                    "6. Send DTMF tones for IVR (send_dtmf)\n"
                    "7. End the call (end_call)\n\n"
                    "Always be helpful and proactive. Use tools when appropriate.\n"
                    "Confirm actions before executing transfers or handoffs."
                ),
                "first_message": (
                    "Hello {{customer_name}}! Welcome to Acme support. "
                    "How can I help you today?"
                ),
                "tools": [
                    # --- Built-in: end_call ---
                    {
                        "name": "end_call",
                        "description": (
                            "End the call when: the customer's question is fully "
                            "resolved, they say goodbye, or there is nothing more "
                            "to assist with."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "enum": [
                                        "resolved",
                                        "customer_requested",
                                        "no_action_needed",
                                    ],
                                    "description": "Why the call is ending",
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "Brief summary of what was discussed",
                                },
                            },
                            "required": ["reason"],
                        },
                    },
                    # --- Built-in: transfer_call ---
                    {
                        "name": "transfer_call",
                        "description": (
                            "Transfer the caller to a human agent when they "
                            "explicitly request it or when the issue requires "
                            "human judgment. Always confirm before transferring."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "target_number": {
                                    "type": "string",
                                    "description": (
                                        "Destination phone number in E.164 format. "
                                        f"Use {args.transfer_number} for general support."
                                    ),
                                },
                                "transfer_mode": {
                                    "type": "string",
                                    "enum": ["warm", "cold"],
                                    "description": (
                                        "warm = brief the agent first, "
                                        "cold = direct connect"
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Why the transfer is happening",
                                },
                            },
                            "required": ["target_number"],
                        },
                    },
                    # --- Built-in: handoff_to_agent ---
                    {
                        "name": "handoff_to_agent",
                        "description": (
                            "Hand off to the billing specialist when the customer "
                            "has billing, payment, invoice, or refund questions."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "agent_id": {
                                    "type": "string",
                                    "description": (
                                        f"Use '{billing_id}' for billing specialist."
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Context for the handoff",
                                },
                                "context": {
                                    "type": "object",
                                    "description": "Data to pass to the new assistant",
                                    "properties": {
                                        "customer_name": {"type": "string"},
                                        "account_id": {"type": "string"},
                                        "issue_summary": {"type": "string"},
                                    },
                                },
                            },
                            "required": ["agent_id"],
                        },
                    },
                    # --- Built-in: send_dtmf ---
                    {
                        "name": "send_dtmf",
                        "description": (
                            "Send DTMF keypad tones. Use when the customer asks "
                            "you to dial an extension, enter a PIN, or navigate "
                            "an IVR menu."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "digits": {
                                    "type": "string",
                                    "description": (
                                        "Digits to send (0-9, *, #). "
                                        "Example: '1234#'"
                                    ),
                                },
                            },
                            "required": ["digits"],
                        },
                    },
                    # --- Webhook: lookup_customer ---
                    {
                        "name": "lookup_customer",
                        "description": (
                            "Look up customer details by phone number or account ID. "
                            "Use when you need to verify identity, check account "
                            "status, or retrieve contact info."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "phone_number": {
                                    "type": "string",
                                    "description": "Phone number in E.164 format",
                                },
                                "account_id": {
                                    "type": "string",
                                    "description": "Account ID (ACC-XXXXX)",
                                },
                            },
                        },
                        "webhook_url": f"{args.webhook_url}/tools/lookup-customer",
                        "timeout_seconds": 15,
                        "max_retries": 2,
                    },
                    # --- Webhook: check_order_status ---
                    {
                        "name": "check_order_status",
                        "description": (
                            "Check the status of an order. Use when the customer "
                            "asks about shipping, delivery, or order progress."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "order_id": {
                                    "type": "string",
                                    "description": "Order ID (ORD-XXX)",
                                },
                                "account_id": {
                                    "type": "string",
                                    "description": (
                                        "Account ID to find their most recent order"
                                    ),
                                },
                            },
                        },
                        "webhook_url": f"{args.webhook_url}/tools/check-order-status",
                        "timeout_seconds": 10,
                    },
                    # --- Webhook: create_ticket ---
                    {
                        "name": "create_ticket",
                        "description": (
                            "Create a support ticket for issues that cannot be "
                            "resolved on this call. Always summarize the issue "
                            "clearly in the description."
                        ),
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "subject": {
                                    "type": "string",
                                    "description": "Brief ticket subject (< 100 chars)",
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Detailed description of the issue"
                                    ),
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high", "urgent"],
                                    "description": "Ticket priority level",
                                },
                            },
                            "required": ["subject", "description"],
                        },
                        "webhook_url": f"{args.webhook_url}/tools/create-ticket",
                        "timeout_seconds": 10,
                    },
                ],
                "stt": {"provider": "deepgram", "model": "nova-3-general", "language": "en"},
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
                "silence_timeout_ms": 1200,
                "interruption_enabled": True,
                "smart_turn_detection": True,
                "smart_turn_stop_secs": 2.5,
                "max_call_duration_seconds": 900,
            },
        },
        token=api_key,
    )
    support_id = result["data"]["id"]
    print(f"   Support Agent: {support_id}")
    api("POST", f"/v1/agents/{support_id}/publish", {}, token=api_key)

    # --- Step 5: Bind phone number with WEBHOOK routing ---
    print("\n5. Binding phone number with webhook routing (call-init)...")
    result = api(
        "POST",
        "/v1/phone-numbers",
        {
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "webhook",
            "server_url": f"{args.webhook_url}/call-init",
        },
        token=api_key,
    )
    print(f"   Bound: {args.twilio_number} → webhook ({args.webhook_url}/call-init)")

    # --- Step 6: Configure Twilio webhooks ---
    print("\n6. Configuring Twilio webhooks...")
    voice_url = f"{args.turncall_url}/webhooks/twilio/voice/inbound"
    status_url = f"{args.turncall_url}/webhooks/twilio/status"

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
        print("   TWILIO credentials not set — configure manually:")
        print(f"   Voice URL:  {voice_url}")
        print(f"   Status URL: {status_url}")

    # --- Step 7: Print webhook server config ---
    print("\n7. Update webhook_server.py with the support agent ID:")
    print(f'   SUPPORT_AGENT_ID = "{support_id}"')
    print(f"\n   Or set env: export SUPPORT_AGENT_ID={support_id}")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Call {args.twilio_number} to talk to the support agent.")
    print("\n  What happens on each call:")
    print("  1. TurnCall POSTs call-init to your webhook server")
    print("  2. Server looks up caller in mock CRM")
    print("  3. Returns assistant_id + variables + knowledge_context")
    print("  4. Agent greets caller by name with full context")
    print("\n  Try saying:")
    print('    "Can you look up my account?"        → lookup_customer tool')
    print('    "What\'s my order status?"            → check_order_status tool')
    print('    "I need to file a complaint"         → create_ticket tool')
    print('    "Transfer me to a person"            → transfer_call (built-in)')
    print('    "I have a billing question"          → handoff_to_agent (built-in)')
    print('    "Dial extension 1234"                → send_dtmf (built-in)')
    print('    "That\'s all, goodbye"               → end_call (built-in)')
    print(f"\n  API Key: {api_key}")
    print(f"  Support Agent ID: {support_id}")
    print(f"  Billing Agent ID: {billing_id}")
    print()


if __name__ == "__main__":
    main()
