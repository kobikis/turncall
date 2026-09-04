"""Speech-to-Speech (S2S) Example — Setup Script

Creates a voice agent using OpenAI Realtime or Gemini Live for
ultra-low latency audio-in/audio-out conversations.

Prerequisites:
  1. Server running: `make docker-up && make run`
  2. .env configured with OPENAI_API_KEY or GOOGLE_API_KEY

Usage:
  # OpenAI Realtime (default)
  python examples/s2s-realtime/setup.py --server-url "http://localhost:8090"

  # Gemini Live
  python examples/s2s-realtime/setup.py \\
    --server-url "http://localhost:8090" \\
    --provider google \\
    --voice Kore

  # Grok voice — presets the Vercel AI Gateway base_url + model + voice.
  # Requires the gateway's wss:// URL in BYOM_ALLOWED_URL_PATTERNS and the
  # gateway key as OPENAI_API_KEY (the backend sends it as the WS bearer).
  python examples/s2s-realtime/setup.py \\
    --server-url "http://localhost:8090" \\
    --provider xai

  # Any other OpenAI-Realtime-compatible gateway (LiteLLM, xAI direct, ...)
  python examples/s2s-realtime/setup.py \\
    --server-url "http://localhost:8090" \\
    --base-url "wss://my-gateway.example/v1/realtime" \\
    --model "xai/grok-voice-think-fast-1.0" \\
    --voice "cosmo"

  # With Twilio phone number
  python examples/s2s-realtime/setup.py \\
    --server-url "https://xxxx.ngrok.io" \\
    --twilio-number "+15559876543" \\
    --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""

import argparse
import fnmatch
import json
import os
import sys

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")

BASE_URL = "http://localhost:8090"

OPENAI_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
GEMINI_VOICES = ("Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Zephyr")

DEFAULT_MODELS = {
    "openai": "gpt-realtime-mini-2025-12-15",
    "google": "models/gemini-3.1-flash-live-preview",
    "xai": "xai/grok-voice-think-fast-1.0",
}

DEFAULT_VOICES = {
    "openai": "alloy",
    "google": "Charon",
    "xai": "cosmo",
}

# "xai" is an example-level shorthand: the backend has no xai S2S provider —
# Grok speaks the OpenAI-Realtime protocol, so it rides provider "openai"
# with a gateway base_url.
DEFAULT_BASE_URLS = {
    "xai": "wss://ai-gateway.vercel.sh/v1/realtime",
}


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


def warn_gateway_env(base_url: str) -> None:
    """Sanity-check the local .env for gateway-mode prerequisites.

    Both are enforced by the server (the allowlist at call start, not agent
    creation), and the server may run with different env than this .env —
    so these warn instead of failing.
    """
    warnings = []

    if os.environ.get("BYOM_ENABLED", "true").strip().lower() in ("false", "0", "no"):
        warnings.append(
            "BYOM_ENABLED is false — the server will reject this base_url at "
            "call start. Set BYOM_ENABLED=true."
        )

    raw_patterns = os.environ.get("BYOM_ALLOWED_URL_PATTERNS", "").strip()
    if raw_patterns:
        try:
            patterns = json.loads(raw_patterns)
        except ValueError:
            patterns = None
        if patterns is None:
            warnings.append(
                f"BYOM_ALLOWED_URL_PATTERNS is not valid JSON: {raw_patterns}"
            )
        elif not any(fnmatch.fnmatch(base_url, p) for p in patterns):
            scheme_host = "/".join(base_url.split("/", 3)[:3])
            warnings.append(
                f"BYOM_ALLOWED_URL_PATTERNS does not match {base_url} — calls "
                f"will fail at pipeline start. Add \"{scheme_host}/*\" to it."
            )
    # An unset/empty allowlist means the server allows any URL (dev mode).

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key or openai_key.startswith("sk-"):
        warnings.append(
            "The backend sends OPENAI_API_KEY as the gateway's WebSocket "
            "bearer — set it to the GATEWAY key (yours looks "
            + ("unset" if not openai_key else "like a real OpenAI sk- key")
            + "). Note: plain-openai S2S agents can't run with a swapped key."
        )

    for msg in warnings:
        print(f"\n  !! WARNING: {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the S2S realtime example")
    parser.add_argument(
        "--server-url",
        required=True,
        help="Public URL where TurnCall is reachable",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "google", "xai"],
        help=(
            "S2S provider (default: openai). xai presets the Vercel AI "
            "Gateway base_url + Grok model/voice (backend provider stays "
            "openai — same Realtime protocol)."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (defaults per provider)",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="Voice name (defaults per provider)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "OpenAI-Realtime-compatible gateway WebSocket (wss://) — e.g. Vercel "
            "AI Gateway or LiteLLM. Enables provider-prefixed models like "
            "xai/grok-voice-think-fast-1.0. Overrides the xai preset; not "
            "supported with google."
        ),
    )
    parser.add_argument(
        "--turn-detection",
        default="server_vad",
        choices=["server_vad", "pipecat_vad"],
        help="Turn detection mode (default: server_vad)",
    )
    parser.add_argument(
        "--twilio-number",
        default=None,
        help="Twilio phone number in E.164 format (optional)",
    )
    parser.add_argument(
        "--twilio-number-sid",
        default=None,
        help="Twilio Phone Number SID (required if --twilio-number is set)",
    )
    args = parser.parse_args()

    if args.twilio_number and not args.twilio_number_sid:
        parser.error("--twilio-number-sid is required when --twilio-number is set")

    if args.base_url and args.provider == "google":
        parser.error("--base-url is only supported with --provider openai or xai")
    if args.base_url and args.provider == "openai" and not args.model:
        # An explicit gateway routes to models we can't guess; xai has a preset.
        parser.error("--model is required with --base-url (e.g. xai/grok-voice-think-fast-1.0)")

    base_url = args.base_url or DEFAULT_BASE_URLS.get(args.provider)
    model = args.model or DEFAULT_MODELS[args.provider]

    if base_url:
        # Gateway mode: the base_url routes to models with their own voice
        # sets, so the OpenAI voice allowlist is off ("cosmo" is Grok's).
        voice = args.voice or "cosmo"
    else:
        voice = args.voice or DEFAULT_VOICES[args.provider]

        # OpenAI's realtime voice set is small + stable, so we can catch typos.
        # Gemini's native-audio voices grow with each model, so we don't gate
        # them — Gemini validates on connect. (GEMINI_VOICES stays as a hint.)
        if args.provider == "openai" and voice not in OPENAI_VOICES:
            print(f"  ERROR: Invalid voice '{voice}' for openai.")
            print(f"  Valid: {', '.join(OPENAI_VOICES)}")
            sys.exit(1)

    print("=" * 60)
    print("  TurnCall — Speech-to-Speech (S2S) Example")
    print("=" * 60)
    print(f"\n  Provider: {args.provider}")
    print(f"  Model:    {model}")
    print(f"  Voice:    {voice}")
    print(f"  Turn:     {args.turn_detection}")
    if base_url:
        print(f"  Gateway:  {base_url}")
        warn_gateway_env(base_url)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "s2s-realtime-demo"})
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

    # --- Step 3: Create S2S assistant ---
    print(f"\n3. Creating S2S assistant ({args.provider}/{voice})...")
    transport = "both" if args.twilio_number else "webrtc"
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": f"s2s-{args.provider}-agent",
            "config": {
                "pipeline_mode": "s2s",
                "system_prompt": (
                    "You are a friendly voice assistant with ultra-low latency.\n\n"
                    "Keep responses concise — you are in a real-time voice "
                    "conversation. Short, natural sentences work best.\n\n"
                    "You can help with general questions, creative tasks, "
                    "and casual conversation. Be warm and expressive."
                ),
                "first_message": "Hey! I'm running in speech-to-speech mode. How can I help?",
                "s2s": {
                    # The backend's S2S enum is openai|google — xai rides the
                    # openai Realtime protocol through the gateway base_url.
                    "provider": "openai" if args.provider == "xai" else args.provider,
                    "model": model,
                    "voice": voice,
                    "turn_detection": args.turn_detection,
                    **({"base_url": base_url} if base_url else {}),
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
                "max_call_duration_seconds": 600,
                "transport": transport,
            },
        },
        token=api_key,
    )
    assistant_id = result["data"]["id"]
    print(f"   Agent: {assistant_id}")

    # Publish
    api("POST", f"/v1/agents/{assistant_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 4: Bind Twilio number (optional) ---
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
        print(f"   Bound: {args.twilio_number} -> s2s-{args.provider}-agent")

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
        else:
            print("   TWILIO credentials not set — configure manually:")
            print(f"   Voice URL:  {voice_url}")
            print(f"   Status URL: {status_url}")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Mode:         S2S ({args.provider})")
    print(f"  Model:        {model}")
    print(f"  Voice:        {voice}")
    print(f"  Agent ID: {assistant_id}")
    print(f"  API Key:      {api_key}")

    if args.twilio_number:
        print(f"\n  Call {args.twilio_number} to talk!")

    print("\n  WebRTC (browser):")
    print("    Open examples/webrtc-client/index.html")
    print(f"    Agent ID: {assistant_id}")
    print()


if __name__ == "__main__":
    main()
