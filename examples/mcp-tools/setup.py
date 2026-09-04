#!/usr/bin/env python3
"""MCP Tools — create an agent that uses MCP server tools.

Usage:
    # Terminal 1: Start the MCP server
    python mcp_server.py

    # Terminal 2: Start TurnCall
    make run

    # Terminal 3: Run setup
    python setup.py \\
        --twilio-number-sid PN_YOUR_SID \\
        --twilio-number +15551234567

    # Call your number — the agent can now look up customers and create tickets
    # via the MCP server (no webhook URLs needed!)
"""

import argparse
import os

import httpx
from dotenv import load_dotenv

# Bootstrap (project/key creation) is platform-gated; read the credential
# from the repo-root .env (real environment variables take precedence).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
PLATFORM_KEY = os.environ.get("PLATFORM_API_KEY", "")


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP tools setup")
    parser.add_argument("--base-url", default="http://localhost:8090")
    parser.add_argument("--mcp-url", default="http://localhost:9002/mcp")
    parser.add_argument("--twilio-number-sid", required=True)
    parser.add_argument("--twilio-number", required=True)
    parser.add_argument("--project-name", default="MCP Tools Demo")
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
    project_id = resp.json()["data"]["id"]
    print(f"  Project: {project_id}")

    # 2. Create API key
    resp = client.post(
        "/v1/api-keys",
        params={"project_id": project_id},
        json={"name": "mcp-demo", "role": "admin"},
    )
    resp.raise_for_status()
    api_key = resp.json()["data"]["raw_key"]
    print(f"  API Key: {api_key}")
    headers = {"Authorization": f"Bearer {api_key}"}

    # 3. Create agent with MCP server
    print("\nCreating agent with MCP tools...")
    resp = client.post(
        "/v1/agents",
        json={
            "name": "mcp-support-agent",
            "config": {
                "system_prompt": (
                    "You are a customer support agent. "
                    "Use the lookup_customer tool to find customer info by phone number. "
                    "Use the create_ticket tool to create support tickets when needed. "
                    "Be professional and helpful."
                ),
                "first_message": "Hello! How can I help you today?",
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "mcp_servers": [
                    {
                        "name": "crm-tools",
                        "transport": "http",
                        "url": args.mcp_url,
                    }
                ],
                "analysis": {
                    "enabled": True,
                    "summary_enabled": True,
                    "sentiment_enabled": True,
                },
            },
        },
        headers=headers,
    )
    resp.raise_for_status()
    agent = resp.json()["data"]
    agent_id = agent["id"]
    print(f"  Agent: {agent_id} (v{agent['version']})")

    # 4. Publish
    resp = client.post(f"/v1/agents/{agent_id}/publish", headers=headers)
    resp.raise_for_status()
    print(f"  Published: v{resp.json()['data']['version']}")

    # 5. Bind phone number
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
    print(f"  Phone: {args.twilio_number} -> agent {agent_id}")

    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nMCP Server: {args.mcp_url}")
    print(f"Agent: {agent_id}")
    print(f"Phone: {args.twilio_number}")
    print(f"\nCall {args.twilio_number} — the agent will:")
    print("  1. Connect to the MCP server at call start")
    print("  2. Discover tools: lookup_customer, create_ticket")
    print("  3. Use them during the conversation as needed")
    print("\nNo webhook URLs needed — tools are self-describing via MCP!")

    print("\n--- How It Works ---")
    print(
        """
Agent config has mcp_servers (not webhook tools):
  "mcp_servers": [{
    "name": "crm-tools",
    "transport": "http",
    "url": "http://localhost:9002/mcp"
  }]

At call start, TurnCall:
  1. Connects to the MCP server
  2. Calls tools/list to discover available tools
  3. Registers them on the LLM alongside built-in tools
  4. Routes tool calls through the MCP protocol

At call end:
  5. Disconnects MCP sessions
"""
    )

    print("--- Transport Options ---")
    print(
        """
HTTP (remote, recommended):
  "transport": "http", "url": "https://mcp.example.com/mcp"

SSE (remote, streaming):
  "transport": "sse", "url": "https://mcp.example.com/sse"

stdio (local, zero latency):
  "transport": "stdio", "command": "python", "args": ["mcp_server.py", "--stdio"]
  Requires: MCP_STDIO_ENABLED=true
"""
    )


if __name__ == "__main__":
    main()
