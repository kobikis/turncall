"""Video Avatar (HeyGen / Tavus) Example — Setup Script

Creates a cascade voice agent with a live video avatar, served over WebRTC to
the browser. Avatar is WebRTC + cascade only.

Prerequisites:
  1. Server running: `make docker-up && make run`
  2. .env with DEEPGRAM_API_KEY, OPENAI_API_KEY, and one of:
       - HEYGEN_LIVE_AVATAR_API_KEY  (HeyGen — from app.liveavatar.com)
       - TAVUS_API_KEY               (Tavus — from platform.tavus.io)
  3. For Tavus: `pip install -e .` so the `tavus` extra (daily-python) is present.

Usage:
  # HeyGen (default)
  python examples/video-avatar/setup.py --avatar-id <liveavatar-id>

  # Tavus (higher quality, lower latency)
  python examples/video-avatar/setup.py --provider tavus --replica-id <replica-id>
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

# Public HeyGen demo avatar (sandbox) — replace with your own.
DEFAULT_AVATAR_ID = "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"


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
        resp = (
            client.post(path, json=data, headers=headers)
            if method == "POST"
            else client.get(path, headers=headers)
        )

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the video avatar example")
    parser.add_argument("--provider", choices=["heygen", "tavus"], default="heygen")
    parser.add_argument("--avatar-id", default=DEFAULT_AVATAR_ID, help="HeyGen LiveAvatar ID")
    parser.add_argument("--replica-id", help="Tavus replica ID (required for --provider tavus)")
    # --sandbox / --no-sandbox (HeyGen). Some avatars are production-only and 400
    # in sandbox mode ("not supported in sandbox mode"); production may charge credits.
    parser.add_argument(
        "--sandbox", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()

    if args.provider == "tavus" and not args.replica_id:
        parser.error("--replica-id is required when --provider tavus")

    if args.provider == "tavus":
        avatar = {"enabled": True, "provider": "tavus", "replica_id": args.replica_id}
        detail = f"Tavus replica {args.replica_id}"
    else:
        avatar = {
            "enabled": True,
            "provider": "heygen",
            "avatar_id": args.avatar_id,
            "is_sandbox": args.sandbox,
        }
        detail = f"HeyGen avatar {args.avatar_id} (sandbox={args.sandbox})"

    print("=" * 60)
    print("  TurnCall — Video Avatar Example")
    print("=" * 60)
    print(f"\n  {detail}")

    print("\n1. Creating project...")
    project_id = api("POST", "/v1/projects", {"name": "video-avatar-demo"})["data"]["id"]
    print(f"   Project: {project_id}")

    print("\n2. Creating API key...")
    api_key = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "setup-key", "role": "admin"},
    )["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    print(f"\n3. Creating cascade agent with {args.provider} avatar...")
    assistant_id = api(
        "POST",
        "/v1/agents",
        {
            "name": "avatar-agent",
            "config": {
                "pipeline_mode": "cascade",
                "system_prompt": (
                    "You are a friendly video avatar assistant. Keep replies "
                    "short and natural — you are on a live video call."
                ),
                "first_message": "Hi! I'm your video avatar. What can I do for you?",
                "transport": "webrtc",
                "avatar": avatar,
            },
        },
        token=api_key,
    )["data"]["id"]
    print(f"   Agent: {assistant_id}")

    api("POST", f"/v1/agents/{assistant_id}/publish", {}, token=api_key)
    print("   Published.")

    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Agent ID: {assistant_id}")
    print(f"  API Key:      {api_key}")
    print("\n  Open examples/webrtc-client/index.html, paste the API key +")
    print("  Agent ID, and Start Call — the avatar appears below the buttons.")
    print()


if __name__ == "__main__":
    main()
