"""Skills example — setup.

Creates a project with three published skills and one support agent that
lists them. The agent's prompt carries only the skills' names and
descriptions; the model loads one with `load_skill` when the conversation
needs it, and its instructions and tools join the call without losing it.

  verify_identity   tool verify_customer
  explain_bill      tool get_invoices, and the billing policy in its instructions
  payment_promise   tool create_payment_promise

Prerequisites:
  1. The stack running and migrated: `make docker-up-local && make migrate-local`
  2. The tools' server running: `uvicorn webhook_server:app --host 0.0.0.0 --port 9100`

Usage:
  python examples/skills/setup.py [--provider openai|xai]
      [--webhook-url http://host.docker.internal:9100]
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

BILLING_POLICY = """Northwind Mobile billing policy

Tariff adjustment: from October 2026 every Unlimited plan costs $12 more a
month. Customers were told by email on 1 September. It cannot be waived.

Late fee: $10 when a bill is paid more than 7 days after its due date. The
first late fee in 12 months can be waived once, as a courtesy, if the
customer asks.

Bills are due on the 15th of the following month.
"""


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

    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        resp = client.request(method, path, json=data, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


def tool(name: str, description: str, properties: dict, path: str, base: str) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        },
        "webhook_url": f"{base}{path}",
        "timeout_seconds": 10,
    }


def skills(webhook_url: str) -> list[dict]:
    return [
        {
            "name": "verify_identity",
            "description": (
                "Use before discussing anything about the caller's account: "
                "balance, bills, charges, payments."
            ),
            "instructions": (
                "Ask for the caller's date of birth and the last 4 digits of "
                "their account number. Call verify_customer with both, the date "
                "as YYYY-MM-DD. Reveal no account detail until it returns "
                "verified=true. Remember the account_id it returns. After two "
                "failed attempts, apologise and say you cannot discuss the "
                "account on this call."
            ),
            "tools": [
                tool(
                    "verify_customer",
                    "Verify the caller against the account.",
                    {
                        "dob": {"type": "string", "description": "YYYY-MM-DD"},
                        "last4": {
                            "type": "string",
                            "description": "Last 4 digits of the account number",
                        },
                    },
                    "/tools/verify-customer",
                    webhook_url,
                ),
            ],
        },
        {
            "name": "explain_bill",
            "description": (
                "Use when a verified caller asks why a bill changed, what a "
                "charge is, or when payment is due."
            ),
            "instructions": (
                "Fetch the invoices with get_invoices and compare the last two. "
                "Name each change with its amount, using the billing policy "
                "below for why it applies. Then give the amount due, the due "
                "date and the payment reference. Keep it to two or three "
                "sentences: this is a phone call.\n\n" + BILLING_POLICY
            ),
            "tools": [
                tool(
                    "get_invoices",
                    "The account's recent invoices, newest last.",
                    {"account_id": {"type": "string"}},
                    "/tools/get-invoices",
                    webhook_url,
                ),
            ],
        },
        {
            "name": "payment_promise",
            "description": (
                "Use when a verified caller cannot pay a bill by its due date "
                "and wants to arrange paying later."
            ),
            "instructions": (
                "Agree a date at most 14 days after the bill's due date and the "
                "amount. Read both back, and on a yes call "
                "create_payment_promise. Tell the caller the promise id and "
                "that no late fee is added while the promise is kept."
            ),
            "tools": [
                tool(
                    "create_payment_promise",
                    "Record a promise to pay.",
                    {
                        "account_id": {"type": "string"},
                        "amount": {"type": "number"},
                        "pay_by": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "/tools/create-payment-promise",
                    webhook_url,
                ),
            ],
        },
    ]


AGENT_CONFIG = {
    "system_prompt": (
        "You are Maya, the support agent at Northwind Mobile, a mobile carrier. "
        "You are on a voice call: keep every reply to one or two short "
        "sentences, and never read out lists or markdown. Today is 30 October "
        "2026. Load a skill as soon as the caller's request needs it, and say "
        "a short line such as 'Let me check that' when you do."
    ),
    "first_message": "Hi, this is Maya from Northwind Mobile. How can I help?",
    "skills": [
        {"skill": "verify_identity"},
        {"skill": "explain_bill"},
        {"skill": "payment_promise"},
    ],
    # Two at once: loading the third unloads the one loaded longest ago.
    "max_loaded_skills": 2,
    "tools": [
        {
            "name": "end_call",
            "description": "End the call once the caller says goodbye.",
            "parameters_schema": {"type": "object", "properties": {}},
        }
    ],
}

# The speech and LLM stack, by which keys the deployment has.
PROVIDERS = {
    "openai": {
        "stt": {"provider": "deepgram", "model": "nova-3-general", "language": "en"},
        "llm": {"provider": "openai", "model": "gpt-5.4-mini"},
        "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
    },
    # One XAI_API_KEY for all three; each role's default model and voice.
    "xai": {
        "stt": {"provider": "xai"},
        "llm": {"provider": "xai"},
        "tts": {"provider": "xai"},
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the skills example")
    parser.add_argument(
        "--webhook-url",
        default="http://host.docker.internal:9100",
        help="Where TurnCall reaches webhook_server.py (default: the host, "
        "from inside the docker stack)",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default="openai",
        help="openai: OpenAI LLM + Deepgram speech (OPENAI_API_KEY, "
        "DEEPGRAM_API_KEY); xai: Grok for all three (XAI_API_KEY)",
    )
    args = parser.parse_args()
    base = args.webhook_url.rstrip("/")

    print("1. Project and API key")
    project_id = api("POST", "/v1/projects", {"name": "skills-demo"})["data"]["id"]
    key = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "skills-demo", "role": "admin"},
    )["data"]["raw_key"]
    print(f"   project {project_id}")

    print("2. Skills (create a draft, publish it)")
    for skill in skills(base):
        row = api("POST", "/v1/skills", skill, token=key)["data"]
        api("POST", f"/v1/skills/{row['id']}/publish", {}, token=key)
        print(f"   {row['name']} v{row['version']} published")

    print("3. Support agent")
    agent = api(
        "POST",
        "/v1/agents",
        {
            "name": "skills-support",
            "config": {**AGENT_CONFIG, **PROVIDERS[args.provider]},
        },
        token=key,
    )["data"]
    api("POST", f"/v1/agents/{agent['id']}/publish", {}, token=key)
    print(f"   agent {agent['id']}")

    print(f"""
Done.

  API key:  {key}
  Agent ID: {agent["id"]}

Talk to it in text:
  .venv/bin/python examples/skills/chat.py --api-key {key} --agent-id {agent["id"]}

Or by voice, in the browser: ./examples/webrtc-client/run.sh, then
Server URL http://localhost:8090 with the key and agent id above.

Callers the mock CRM knows: last 4 digits 4471, born 1985-03-03 (Jane Doe),
or 2210, born 1990-07-14 (Sam Lee).
""")


if __name__ == "__main__":
    main()
