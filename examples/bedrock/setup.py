"""AWS Bedrock / Nova Sonic Example — Setup Script

Creates a voice agent backed by AWS: either a cascade agent whose LLM leg is
Bedrock, or an S2S agent running Amazon Nova Sonic 2.

Credentials are never put in the agent config by this script. It relies on the
ambient AWS chain (env vars, an SSO profile, an instance profile, IRSA) or on
--role-arn, which TurnCall assumes at call time and which stores no durable
secret. See adr/0016.

Prerequisites:
  1. Server running: `make docker-up-local && make migrate-local`
  2. AWS credentials reachable by the *server* process, or a --role-arn it can
     assume. `aws sts get-caller-identity` is the quickest way to confirm.
  3. Model access granted in the Bedrock console for the region you pick —
     availability is region-specific and access is per-model opt-in.

Usage:
  # Bedrock LLM in a cascade pipeline (Deepgram STT/TTS around it)
  python examples/bedrock/setup.py --server-url "http://localhost:8090"

  # ...in a specific region, with an explicit model
  python examples/bedrock/setup.py \\
    --server-url "http://localhost:8090" \\
    --region eu-central-1 \\
    --model "us.anthropic.claude-haiku-4-5-20251001-v1:0"

  # Nova Sonic 2 (speech-to-speech, no STT/TTS in the pipeline)
  python examples/bedrock/setup.py \\
    --server-url "http://localhost:8090" --mode s2s --voice matthew

  # Assume a role per call instead of using the server's own credentials
  python examples/bedrock/setup.py \\
    --server-url "http://localhost:8090" \\
    --role-arn "arn:aws:iam::123456789012:role/turncall-bedrock"

  # With a Twilio phone number
  python examples/bedrock/setup.py \\
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

# Cross-region inference profiles (the "us."/"eu." prefixes) are usually what
# you want in production — they fail over between regions in the geography.
# Provisioned-throughput ARNs work here too; the model id passes through
# verbatim, TurnCall does not validate it.
# Verified working on-demand. Newer Anthropic models on Bedrock CANNOT be
# invoked by their bare id — they require a cross-region inference profile, which
# is what the "us." prefix is. Outside the US, use the "eu." or "apac." prefix
# for the matching geography.
DEFAULT_LLM_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_S2S_MODEL = "amazon.nova-2-sonic-v1:0"
NOVA_SONIC_VOICES = ("matthew", "tiffany", "amy", "lupe", "carlos")


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
    parser = argparse.ArgumentParser(description="Set up the AWS Bedrock example")
    parser.add_argument(
        "--server-url", required=True, help="Public URL where TurnCall is reachable"
    )
    parser.add_argument(
        "--mode",
        default="llm",
        choices=["llm", "s2s"],
        help=(
            "llm = Bedrock as the LLM leg of a cascade pipeline (default); "
            "s2s = Amazon Nova Sonic 2 speech-to-speech"
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region for Bedrock. Defaults to the server's AWS_REGION, which "
            "also points at the S3 bucket — set this explicitly, since model "
            "availability rarely matches where your bucket lives."
        ),
    )
    parser.add_argument("--model", default=None, help="Bedrock model id or ARN")
    parser.add_argument(
        "--voice", default=None, help=f"Nova Sonic voice ({', '.join(NOVA_SONIC_VOICES)})"
    )
    parser.add_argument(
        "--endpointing-sensitivity",
        default=None,
        choices=["LOW", "MEDIUM", "HIGH"],
        help="How quickly Nova Sonic 2 decides the caller stopped speaking (s2s only)",
    )
    parser.add_argument(
        "--role-arn",
        default=None,
        help=(
            "IAM role TurnCall assumes per call. Preferred over static keys: it "
            "yields temporary credentials and persists no secret."
        ),
    )
    parser.add_argument("--external-id", default=None, help="ExternalId for --role-arn")
    parser.add_argument(
        "--twilio-number", default=None, help="Twilio number in E.164 (optional)"
    )
    parser.add_argument(
        "--twilio-number-sid",
        default=None,
        help="Twilio Phone Number SID (required with --twilio-number)",
    )
    args = parser.parse_args()

    if args.twilio_number and not args.twilio_number_sid:
        parser.error("--twilio-number-sid is required when --twilio-number is set")
    if args.external_id and not args.role_arn:
        parser.error("--external-id only applies with --role-arn")
    if args.mode == "llm" and (args.voice or args.endpointing_sensitivity):
        parser.error("--voice/--endpointing-sensitivity apply to --mode s2s only")

    is_s2s = args.mode == "s2s"
    model = args.model or (DEFAULT_S2S_MODEL if is_s2s else DEFAULT_LLM_MODEL)
    voice = args.voice or "matthew"

    if is_s2s and voice not in NOVA_SONIC_VOICES:
        print(f"  WARNING: '{voice}' is not a known Nova Sonic voice.")
        print(f"  Known: {', '.join(NOVA_SONIC_VOICES)} — AWS validates on connect.")

    # Only sent when set; otherwise the agent inherits the server's AWS_REGION.
    aws_block: dict = {}
    if args.region:
        aws_block["region"] = args.region
    if args.role_arn:
        aws_block["role_arn"] = args.role_arn
    if args.external_id:
        aws_block["external_id"] = args.external_id

    print("=" * 60)
    print("  TurnCall — AWS Bedrock / Nova Sonic Example")
    print("=" * 60)
    print(f"\n  Mode:   {'Nova Sonic 2 (s2s)' if is_s2s else 'Bedrock LLM (cascade)'}")
    print(f"  Model:  {model}")
    print(f"  Region: {args.region or '(server default — AWS_REGION)'}")
    print(f"  Creds:  {'assume ' + args.role_arn if args.role_arn else 'ambient chain'}")
    if is_s2s:
        print(f"  Voice:  {voice}")

    print("\n1. Creating project...")
    project_id = api("POST", "/v1/projects", {"name": "bedrock-demo"})["data"]["id"]
    print(f"   Project: {project_id}")

    print("\n2. Creating API key...")
    api_key = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "setup-key", "role": "admin"},
    )["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    print(f"\n3. Creating agent ({'nova-sonic' if is_s2s else 'bedrock'})...")
    config: dict = {
        "system_prompt": (
            "You are a friendly voice assistant running on AWS.\n\n"
            "Keep responses short and natural — this is a spoken conversation."
        ),
        "first_message": "Hi! I'm running on AWS. What can I do for you?",
        "transport": "both" if args.twilio_number else "webrtc",
        "max_call_duration_seconds": 600,
    }
    if aws_block:
        config["aws"] = aws_block

    if is_s2s:
        config["pipeline_mode"] = "s2s"
        s2s: dict = {"provider": "aws", "model": model, "voice": voice}
        if args.endpointing_sensitivity:
            # No home in turn_detection's server_vad|pipecat_vad enum, so it
            # rides in extra. Nova Sonic 2 only. See adr/0016.
            s2s["extra"] = {"endpointing_sensitivity": args.endpointing_sensitivity}
        config["s2s"] = s2s
    else:
        config["llm"] = {"provider": "bedrock", "model": model}

    agent_id = api(
        "POST",
        "/v1/agents",
        {"name": f"bedrock-{args.mode}-agent", "config": config},
        token=api_key,
    )["data"]["id"]
    print(f"   Agent: {agent_id}")

    api("POST", f"/v1/agents/{agent_id}/publish", {}, token=api_key)
    print("   Published.")

    if args.twilio_number:
        print(f"\n4. Binding Twilio number {args.twilio_number}...")
        api(
            "POST",
            "/v1/phone-numbers",
            {
                "external_number_sid": args.twilio_number_sid,
                "e164_number": args.twilio_number,
                "routing_target_type": "agent",
                "routing_target_id": agent_id,
            },
            token=api_key,
        )
        print("   Bound.")

    print("\n" + "=" * 60)
    print("  Ready")
    print("=" * 60)
    if args.twilio_number:
        print(f"\n  Call {args.twilio_number}")
    else:
        print("\n  Talk to it in the browser:")
        print("    cd examples/webrtc-client && npm install && npm run dev")
        print("    then open http://localhost:5174 and paste:")
    print(f"\n  API key:  {api_key}")
    print(f"  Agent id: {agent_id}")
    print(
        "\n  If calls fail with an AccessDenied or ValidationException, the "
        "usual cause is\n  model access not granted in the Bedrock console for "
        "this model/region pair."
    )


if __name__ == "__main__":
    main()
