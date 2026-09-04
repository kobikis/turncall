"""OpenRouter Fallback Routing Example — Setup Script

Creates a voice agent that runs on OpenRouter with model fallback routing:
if the primary model rate-limits or errors mid-call, OpenRouter fails over to
the next model in `fallback_models`, in order. The model that actually answered
each turn is recorded on the call's transcript.final events.

OpenRouter is voice-only (WebRTC / Twilio / WhatsApp voice). It is NOT supported
on the SMS/Chat text path. See adr/0003-openrouter-provider.md.

Prerequisites:
  1. Server running with OPENROUTER_API_KEY set in .env (get one at
     openrouter.ai/keys). A platform-level key is used by default; pass
     --llm-api-key to override per-agent.
  2. make docker-up && make run
  3. .env configured with DEEPGRAM_API_KEY

Usage:
  # WebRTC only (browser calls):
  python examples/openrouter-fallback/setup.py --server-url "http://localhost:8090"

  # Custom primary + fallback chain:
  python examples/openrouter-fallback/setup.py \\
    --server-url "http://localhost:8090" \\
    --llm-model "anthropic/claude-3.5-sonnet" \\
    --fallback "openai/gpt-4o" --fallback "google/gemini-flash-1.5"

  # With a Twilio phone number:
  python examples/openrouter-fallback/setup.py \\
    --server-url "https://xxxx.ngrok.io" \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
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
    parser = argparse.ArgumentParser(description="Set up the OpenRouter fallback example")
    parser.add_argument(
        "--server-url",
        required=True,
        help="Public URL where TurnCall is reachable (e.g., https://abc123.ngrok.io)",
    )
    parser.add_argument(
        "--llm-model",
        default="anthropic/claude-3.5-sonnet",
        help="Primary OpenRouter model (default: anthropic/claude-3.5-sonnet)",
    )
    parser.add_argument(
        "--fallback",
        action="append",
        default=None,
        help="Fallback model, in order (repeatable). "
        "Default: openai/gpt-4o, google/gemini-flash-1.5",
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help="Per-agent OpenRouter key (overrides the server's OPENROUTER_API_KEY)",
    )
    parser.add_argument(
        "--twilio-number",
        default=None,
        help="Twilio phone number in E.164 format (optional, for phone calls)",
    )
    parser.add_argument(
        "--twilio-number-sid",
        default=None,
        help="Twilio Phone Number SID (required if --twilio-number is set)",
    )
    args = parser.parse_args()

    if args.twilio_number and not args.twilio_number_sid:
        parser.error("--twilio-number-sid is required when --twilio-number is set")

    fallbacks = args.fallback or ["openai/gpt-4o", "google/gemini-flash-1.5"]

    print("=" * 60)
    print("  TurnCall — OpenRouter Fallback Routing Example")
    print("=" * 60)
    print(f"\n  Primary:   {args.llm_model}")
    for i, fb in enumerate(fallbacks, 1):
        print(f"  Fallback {i}: {fb}")

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "openrouter-fallback-demo"})
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

    # --- Step 3: Build LLM config ---
    # fallback_models is OpenRouter's `models` array — tried in order after the
    # primary `model`. Only valid when provider == "openrouter".
    llm_config: dict = {
        "provider": "openrouter",
        "model": args.llm_model,
        "fallback_models": fallbacks,
    }
    if args.llm_api_key:
        llm_config["api_key"] = args.llm_api_key

    # --- Step 4: Create assistant ---
    print("\n3. Creating assistant...")
    transport = "both" if args.twilio_number else "webrtc"
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "openrouter-agent",
            "config": {
                "system_prompt": (
                    "You are a friendly AI assistant routed through OpenRouter.\n\n"
                    "Keep your responses concise — you are in a voice conversation, "
                    "so short sentences work best.\n\n"
                    "If you don't know something, say so honestly."
                ),
                "first_message": (
                    "Hey! I'm running through OpenRouter with model fallback. "
                    "What would you like to talk about?"
                ),
                "llm": llm_config,
                "stt": {"provider": "deepgram", "model": "nova-3-general", "language": "en"},
                "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
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
                "silence_timeout_ms": 1000,
                "interruption_enabled": True,
                "smart_turn_detection": True,
                "smart_turn_stop_secs": 3.0,
                "max_call_duration_seconds": 600,
                "transport": transport,
            },
        },
        token=api_key,
    )
    assistant_id = result["data"]["id"]
    print(f"   Agent: {assistant_id}")

    # Publish it
    api("POST", f"/v1/agents/{assistant_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 5: Bind Twilio number (optional) ---
    if args.twilio_number:
        print(f"\n4. Binding Twilio number {args.twilio_number}...")
        api(
            "POST",
            "/v1/phone-numbers",
            {
                "external_number_sid": args.twilio_number_sid,
                "e164_number": args.twilio_number,
                "routing_target_type": "agent",
                "routing_target_id": assistant_id,
            },
            token=api_key,
        )
        print(f"   Bound: {args.twilio_number} -> openrouter-agent")

        print("\n5. Configuring Twilio webhooks...")
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
            print("   TWILIO credentials not set — configure manually:")
            print(f"   Voice URL:  {voice_url}")
            print(f"   Status URL: {status_url}")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)

    print(f"\n  Primary model: {args.llm_model}")
    print(f"  Fallback chain: {' -> '.join(fallbacks)}")
    print(f"\n  Agent ID: {assistant_id}")
    print(f"  API Key: {api_key}")

    if args.twilio_number:
        print(f"\n  Call {args.twilio_number} to talk to your OpenRouter agent!")

    print("\n  WebRTC (browser):")
    print("    Open examples/webrtc-client/index.html")
    print(f"    Agent ID: {assistant_id}")
    print("\n  Tip: which model answered each turn is on the call's")
    print("       transcript.final events (payload.model).")
    print()


if __name__ == "__main__":
    main()
