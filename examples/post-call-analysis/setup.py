#!/usr/bin/env python3
"""Post-call structured analysis example.

Creates an agent with analysis configured, makes a test call,
and retrieves the analysis results.

Usage:
    # 1. Start turncall: make run
    # 2. Run this script:
    python examples/post-call-analysis/setup.py \\
        --twilio-number-sid PN_YOUR_SID \\
        --twilio-number +15551234567

    # The script will:
    # - Create a project and API key
    # - Create an agent with analysis config (summary + sentiment + success eval + extraction)
    # - Bind your Twilio phone number to the agent
    # - Show how to retrieve analysis after a call ends

Requirements:
    pip install httpx
"""

import argparse
import json
import os

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")

BASE_URL = "http://localhost:8090"


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-call analysis setup")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--project-name", default="Analysis Demo")
    parser.add_argument(
        "--twilio-number-sid",
        required=True,
        help="Twilio Phone Number SID (PN...)",
    )
    parser.add_argument(
        "--twilio-number",
        required=True,
        help="Twilio phone number in E.164 format (+15551234567)",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    if not PLATFORM_KEY:
        raise SystemExit(
            "PLATFORM_API_KEY is not set — add it to .env (see env.example)"
        )
    client = httpx.Client(
        base_url=base, timeout=30.0, headers={"X-Platform-Key": PLATFORM_KEY}
    )

    # 1. Create project
    print("Creating project...")
    resp = client.post("/v1/projects", json={"name": args.project_name})
    resp.raise_for_status()
    project = resp.json()["data"]
    project_id = project["id"]
    print(f"  Project: {project_id}")

    # 2. Create API key
    print("Creating API key...")
    resp = client.post(
        "/v1/api-keys",
        params={"project_id": project_id},
        json={"name": "analysis-demo", "role": "admin"},
    )
    resp.raise_for_status()
    api_key = resp.json()["data"]["raw_key"]
    print(f"  API Key: {api_key[:12]}...")

    headers = {"Authorization": f"Bearer {api_key}"}

    # 3. Create agent with full analysis config
    print("\nCreating agent with post-call analysis...")
    agent_config = {
        "name": "support-agent",
        "config": {
            "system_prompt": (
                "You are a customer support agent for TechCorp. "
                "Help customers with their technical issues. "
                "Be professional, empathetic, and solution-oriented."
            ),
            "first_message": "Hello! Welcome to TechCorp support. How can I help you today?",
            "llm": {"provider": "openai", "model": "gpt-4o-mini"},
            "analysis": {
                "enabled": True,
                "summary_enabled": True,
                "summary_prompt": (
                    "Summarize this support call in 2-3 sentences. "
                    "Include: the customer's issue, what was discussed, and the outcome."
                ),
                "success_evaluation": {
                    "enabled": True,
                    "rubric": (
                        "The call is successful if: "
                        "1) The customer's issue was identified, "
                        "2) A solution or next step was provided, "
                        "3) The customer seemed satisfied with the resolution."
                    ),
                    "scale": "pass_fail",
                },
                "sentiment_enabled": True,
                "structured_extraction_schema": {
                    "type": "object",
                    "properties": {
                        "customer_issue": {
                            "type": "string",
                            "description": "Brief description of the customer's issue",
                        },
                        "resolution": {
                            "type": "string",
                            "description": "How the issue was resolved or next steps",
                        },
                        "product_mentioned": {
                            "type": "string",
                            "description": "Product or service discussed",
                        },
                        "follow_up_needed": {
                            "type": "boolean",
                            "description": "Whether a follow-up is needed",
                        },
                    },
                    "required": ["customer_issue", "resolution"],
                },
                "scoring_rubric": {
                    "professionalism": {
                        "max_score": 10,
                        "description": "Was the agent professional and courteous?",
                    },
                    "problem_solving": {
                        "max_score": 10,
                        "description": "Did the agent effectively identify and address the issue?",
                    },
                    "communication": {
                        "max_score": 10,
                        "description": "Was the agent clear and easy to understand?",
                    },
                },
            },
        },
    }

    resp = client.post("/v1/agents", json=agent_config, headers=headers)
    resp.raise_for_status()
    agent = resp.json()["data"]
    agent_id = agent["id"]
    print(f"  Agent: {agent_id} (v{agent['version']}, {agent['state']})")

    # 4. Publish agent
    print("  Publishing agent...")
    resp = client.post(f"/v1/agents/{agent_id}/publish", headers=headers)
    resp.raise_for_status()
    print(f"  Agent published: v{resp.json()['data']['version']}")

    # 5. Bind phone number to agent
    print("\nBinding phone number to agent...")
    resp = client.post(
        "/v1/phone-numbers",
        json={
            "external_number_sid": args.twilio_number_sid,
            "e164_number": args.twilio_number,
            "routing_target_type": "agent",
            "routing_target_id": agent_id,
        },
        headers=headers,
    )
    resp.raise_for_status()
    phone = resp.json()["data"]
    print(f"  Phone: {phone['e164_number']} → agent {agent_id}")

    # 6. Show analysis retrieval
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nProject ID: {project_id}")
    print(f"Agent ID:   {agent_id}")
    print(f"API Key:    {api_key}")

    print("\n--- How Post-Call Analysis Works ---")
    print(
        """
1. When a call ends, TurnCall automatically runs analysis in the background (~2-5s).
2. A single call.ended webhook fires once analysis (and the recording) finish —
   the structured analysis results are included inline in its payload.

3. Retrieve analysis for any completed call:

   # Get analysis results
   curl -H "Authorization: Bearer {api_key}" \\
        {base}/v1/calls/{{call_id}}/analysis

   # Response (when complete):
   {{
     "success": true,
     "data": {{
       "call_id": "...",
       "status": "completed",
       "analysis": {{
         "summary": "Customer called about a login issue...",
         "success_evaluation": {{"score": "pass", "reason": "Issue resolved"}},
         "sentiment": {{"overall": "positive", "customer_satisfaction": "satisfied"}},
         "structured_data": {{
           "customer_issue": "Cannot log in to dashboard",
           "resolution": "Password reset link sent",
           "product_mentioned": "TechCorp Dashboard",
           "follow_up_needed": false
         }},
         "scoring": {{
           "professionalism": {{"score": 9, "reason": "Very courteous"}},
           "problem_solving": {{"score": 8, "reason": "Quick resolution"}},
           "communication": {{"score": 9, "reason": "Clear explanations"}}
         }},
         "model": "gpt-4o-mini",
         "duration_ms": 2350,
         "analyzed_at": "2026-04-17T10:00:00Z"
       }}
     }}
   }}

4. Re-run analysis (e.g., after changing agent config):

   curl -X POST -H "Authorization: Bearer {api_key}" \\
        {base}/v1/calls/{{call_id}}/analysis/rerun

5. Analysis is also included in the call response:

   curl -H "Authorization: Bearer {api_key}" \\
        {base}/v1/calls/{{call_id}}
""".format(
            api_key=api_key[:12] + "...", base=base
        )
    )

    # 6. Show example with different scale types
    print("--- Analysis Config Variants ---\n")

    print("Likert scale (1-5):")
    print(
        json.dumps(
            {
                "analysis": {
                    "success_evaluation": {"enabled": True, "scale": "likert"},
                }
            },
            indent=2,
        )
    )

    print("\nNumeric scale (0-100):")
    print(
        json.dumps(
            {
                "analysis": {
                    "success_evaluation": {"enabled": True, "scale": "numeric"},
                }
            },
            indent=2,
        )
    )

    print("\nMinimal (summary only):")
    print(
        json.dumps(
            {
                "analysis": {"enabled": True, "summary_enabled": True},
            },
            indent=2,
        )
    )

    print("\nCustom model for analysis:")
    print(
        json.dumps(
            {
                "analysis": {"enabled": True, "model": "gpt-4o"},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
