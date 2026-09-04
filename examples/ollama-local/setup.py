"""Ollama Local LLM Example — Setup Script

Creates a voice agent powered by a local LLM running via Ollama.
No OpenAI API key required for the LLM.

Prerequisites:
  1. Ollama running with a model: `ollama pull gemma3:12b`
  2. Server running: `make docker-up && make run`
  3. .env configured with DEEPGRAM_API_KEY

Usage:
  # WebRTC only (browser calls):
  python examples/ollama-local/setup.py --server-url "http://localhost:8090"

  # With Twilio phone number:
  python examples/ollama-local/setup.py \\
    --server-url "https://xxxx.ngrok.io" \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

  # With a remote OpenAI-compatible endpoint:
  python examples/ollama-local/setup.py \\
    --server-url "http://localhost:8090" \\
    --llm-provider custom_openai \\
    --llm-model "meta-llama/Llama-3-70b-chat-hf" \\
    --llm-base-url "https://api.together.xyz/v1" \\
    --llm-api-key "your-together-key"
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


def check_ollama(base_url: str, model: str) -> None:
    """Verify Ollama is reachable and the model is available."""
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{base_url.rstrip('/v1')}/api/tags")
            if resp.status_code != 200:
                print(f"  WARNING: Ollama returned {resp.status_code}")
                return
            models = [m["name"] for m in resp.json().get("models", [])]
            # Check for exact match or prefix match (e.g., "gemma3:12b" matches "gemma3:12b")
            if not any(model == m or model == m.split(":")[0] for m in models):
                print(f"  WARNING: Model '{model}' not found in Ollama.")
                print(f"  Available: {', '.join(models) or '(none)'}")
                print(f"  Run: ollama pull {model}")
    except httpx.ConnectError:
        print(f"  WARNING: Cannot reach Ollama at {base_url}")
        print("  Make sure Ollama is running: ollama serve")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the Ollama local LLM example")
    parser.add_argument(
        "--server-url",
        required=True,
        help="Public URL where TurnCall is reachable (e.g., https://abc123.ngrok.io)",
    )
    parser.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "custom_openai"],
        help="LLM provider (default: ollama)",
    )
    parser.add_argument(
        "--llm-model",
        default="gemma3:12b",
        help="Model name (default: gemma3:12b)",
    )
    parser.add_argument(
        "--llm-base-url",
        default=None,
        help="Custom base URL (default: http://localhost:11434/v1 for ollama)",
    )
    parser.add_argument(
        "--llm-api-key",
        default=None,
        help="API key for custom_openai provider",
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

    print("=" * 60)
    print("  TurnCall — Local LLM Example (BYOM)")
    print("=" * 60)

    # --- Pre-flight: Check Ollama ---
    if args.llm_provider == "ollama":
        base_url = args.llm_base_url or "http://localhost:11434/v1"
        print(f"\n0. Checking Ollama at {base_url}...")
        check_ollama(base_url, args.llm_model)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "ollama-local-demo"})
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
    llm_config: dict = {
        "provider": args.llm_provider,
        "model": args.llm_model,
    }
    if args.llm_base_url:
        llm_config["base_url"] = args.llm_base_url
    if args.llm_api_key:
        llm_config["api_key"] = args.llm_api_key

    # --- Step 4: Create assistant ---
    print(f"\n3. Creating assistant (LLM: {args.llm_provider}/{args.llm_model})...")
    transport = "both" if args.twilio_number else "webrtc"
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "local-llm-agent",
            "config": {
                "system_prompt": (
                    "You are a friendly AI assistant powered by a local language model.\n\n"
                    "Keep your responses concise — you are in a voice conversation, "
                    "so short sentences work best.\n\n"
                    "You can help with:\n"
                    "- General knowledge questions\n"
                    "- Creative writing and brainstorming\n"
                    "- Technical explanations\n"
                    "- Casual conversation\n\n"
                    "If you don't know something, say so honestly."
                ),
                "first_message": (
                    "Hey! I'm running on a local model. "
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
        print(f"   Bound: {args.twilio_number} -> local-llm-agent")

        # Configure Twilio webhooks
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

    print(f"\n  LLM: {args.llm_provider}/{args.llm_model}")
    if args.llm_base_url:
        print(f"  Endpoint: {args.llm_base_url}")

    print(f"\n  Agent ID: {assistant_id}")
    print(f"  API Key: {api_key}")

    if args.twilio_number:
        print(f"\n  Call {args.twilio_number} to talk to your local model!")

    print("\n  WebRTC (browser):")
    print("    Open examples/webrtc-client/index.html")
    print(f"    Agent ID: {assistant_id}")
    print()


if __name__ == "__main__":
    main()
