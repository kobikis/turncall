"""Knowledge Base Example — Complete Setup Script

Demonstrates the three knowledge base retrieval modes:
  1. prompt — small FAQ injected entirely into system prompt
  2. auto   — per-turn RAG retrieval (semantic search on every user message)
  3. tool   — LLM decides when to query the knowledge base

Prerequisites:
  1. Server running: `make docker-up && make run`
  2. PostgreSQL has pgvector extension: `CREATE EXTENSION IF NOT EXISTS vector;`
  3. .env configured with OPENAI_API_KEY (for embeddings)

Usage:
  python examples/knowledge-base/setup.py \\
    --server-url "https://your-ngrok-url.ngrok.io"

Then test via the Chat API:
  curl -X POST http://localhost:8090/v1/chat \\
    -H "Authorization: Bearer <api-key>" \\
    -H "Content-Type: application/json" \\
    -d '{"agent_id": "<agent-id>", "message": "What are your business hours?"}'
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

# Sample documents for each mode
FAQ_DOCUMENT = """
Acme Corp FAQ

Q: What are your business hours?
A: We are open Monday through Friday, 9am to 6pm Eastern Time.

Q: What is your return policy?
A: You can return any item within 30 days of purchase for a full refund.

Q: How do I contact support?
A: Email support@acmecorp.com or call 1-800-ACME-HELP.

Q: Where are you located?
A: Our headquarters is at 123 Innovation Drive, Austin, TX 78701.
""".strip()

PRODUCT_CATALOG = """
Acme Widget Pro - $49.99
Our flagship widget with 10x processing power. Features include:
- Real-time data synchronization
- Cloud-based dashboard
- API integration support
- 99.9% uptime SLA

Acme Widget Lite - $19.99
Perfect for small teams. Features include:
- Basic data synchronization
- Web dashboard
- Email support
- 99% uptime SLA

Acme Widget Enterprise - $199.99/month
For large organizations. Features include:
- Unlimited data synchronization
- Custom dashboard with SSO
- Dedicated API endpoints
- Priority 24/7 support
- Custom SLA

Acme Analytics Add-on - $29.99/month
Real-time analytics for any Widget plan:
- Usage dashboards
- Anomaly detection
- Weekly email reports
- Export to CSV/JSON
""".strip()

TROUBLESHOOTING_GUIDE = """
Troubleshooting Guide

## Widget Won't Connect
1. Check that your API key is valid in Settings > API Keys
2. Ensure your firewall allows outbound connections on port 443
3. Try regenerating your API key
4. If using a proxy, add *.acmewidget.com to your allowlist

## Slow Performance
1. Check your internet connection speed (minimum 10 Mbps recommended)
2. Reduce the sync frequency in Settings > Sync
3. Clear the local cache: Settings > Advanced > Clear Cache
4. Contact support if issue persists after cache clear

## Data Not Syncing
1. Verify the source connection in Settings > Connections
2. Check the sync log for errors: Dashboard > Logs
3. Ensure the source data format matches the expected schema
4. Try a manual sync: Dashboard > Sync Now

## Billing Issues
1. Check your payment method in Settings > Billing
2. Ensure your card has not expired
3. Contact billing@acmecorp.com for invoice questions
4. Downgrade/upgrade plans at Settings > Subscription
""".strip()


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

    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        if method == "POST":
            resp = client.post(path, json=data, headers=headers)
        elif method == "PUT":
            resp = client.put(path, json=data, headers=headers)
        elif method == "DELETE":
            resp = client.delete(path, headers=headers)
        else:
            resp = client.get(path, headers=headers)

    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    if resp.status_code == 204:
        return {"success": True}
    return resp.json()


def upload_document(
    path: str, kb_id: str, filename: str, content: str, token: str
) -> dict:
    """Upload a text document to a knowledge base."""
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, content.encode(), "text/plain")}
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        resp = client.post(
            f"{path}/{kb_id}/documents",
            files=files,
            headers=headers,
        )
    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the knowledge base example")
    parser.add_argument(
        "--server-url",
        default="http://localhost:8090",
        help="Public URL where TurnCall is reachable",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  TurnCall Knowledge Base Example — Setup")
    print("=" * 60)

    # --- Step 1: Create project ---
    print("\n1. Creating project...")
    result = api("POST", "/v1/projects", {"name": "kb-demo"})
    project_id = result["data"]["id"]
    print(f"   Project: {project_id}")

    # --- Step 2: Create API key ---
    print("\n2. Creating API key...")
    result = api(
        "POST",
        f"/v1/api-keys?project_id={project_id}",
        {"name": "kb-setup-key", "role": "admin"},
    )
    api_key = result["data"]["raw_key"]
    print(f"   API Key: {api_key[:20]}...")

    # --- Step 3: Create knowledge bases (one per mode) ---
    print("\n3. Creating knowledge bases...")

    # KB for prompt mode (small FAQ)
    result = api(
        "POST",
        "/v1/knowledge-bases",
        {"name": "acme-faq", "description": "Small FAQ — injected fully into prompt"},
        token=api_key,
    )
    faq_kb_id = result["data"]["id"]
    print(f"   FAQ KB (prompt mode): {faq_kb_id}")

    # KB for auto mode (product catalog)
    result = api(
        "POST",
        "/v1/knowledge-bases",
        {"name": "product-catalog", "description": "Product info — auto RAG retrieval"},
        token=api_key,
    )
    catalog_kb_id = result["data"]["id"]
    print(f"   Catalog KB (auto mode): {catalog_kb_id}")

    # KB for tool mode (troubleshooting)
    result = api(
        "POST",
        "/v1/knowledge-bases",
        {
            "name": "troubleshooting",
            "description": "Tech support docs — LLM queries on demand",
        },
        token=api_key,
    )
    troubleshoot_kb_id = result["data"]["id"]
    print(f"   Troubleshooting KB (tool mode): {troubleshoot_kb_id}")

    # --- Step 4: Upload documents ---
    print("\n4. Uploading documents...")

    result = upload_document(
        "/v1/knowledge-bases", faq_kb_id, "faq.txt", FAQ_DOCUMENT, api_key
    )
    print(
        f"   FAQ doc: {result['data']['id']} ({result['data']['chunk_count']} chunks)"
    )

    result = upload_document(
        "/v1/knowledge-bases", catalog_kb_id, "products.txt", PRODUCT_CATALOG, api_key
    )
    print(
        f"   Catalog doc: {result['data']['id']} ({result['data']['chunk_count']} chunks)"
    )

    result = upload_document(
        "/v1/knowledge-bases",
        troubleshoot_kb_id,
        "troubleshooting.txt",
        TROUBLESHOOTING_GUIDE,
        api_key,
    )
    print(
        f"   Troubleshoot doc: {result['data']['id']} ({result['data']['chunk_count']} chunks)"
    )

    # --- Step 5: Create agent ---
    print("\n5. Creating support agent...")
    result = api(
        "POST",
        "/v1/agents",
        {
            "name": "acme-support",
            "config": {
                "system_prompt": (
                    "You are a friendly customer support agent for Acme Corp.\n\n"
                    "Help customers with:\n"
                    "- General questions (hours, location, policies)\n"
                    "- Product information and recommendations\n"
                    "- Technical troubleshooting\n\n"
                    "Be concise and helpful. If you don't know something, "
                    "say so rather than guessing."
                ),
                "first_message": "Hi! I'm the Acme Corp support agent. How can I help you today?",
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "stt": {"provider": "deepgram", "model": "nova-3-general", "language": "en"},
                "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
            },
        },
        token=api_key,
    )
    agent_id = result["data"]["id"]
    print(f"   Agent: {agent_id}")

    # Publish it
    api("POST", f"/v1/agents/{agent_id}/publish", {}, token=api_key)
    print("   Published.")

    # --- Step 6: Link knowledge bases to agent ---
    print("\n6. Linking knowledge bases to agent...")

    # FAQ — prompt mode (small, always in context)
    api(
        "POST",
        f"/v1/agents/{agent_id}/knowledge-bases",
        {
            "knowledge_base_id": faq_kb_id,
            "mode": "prompt",
            "priority": 0,
        },
        token=api_key,
    )
    print("   Linked FAQ KB → prompt mode")

    # Product catalog — auto mode (RAG on every turn)
    api(
        "POST",
        f"/v1/agents/{agent_id}/knowledge-bases",
        {
            "knowledge_base_id": catalog_kb_id,
            "mode": "auto",
            "priority": 1,
            "top_k": 3,
            # Hybrid retrieval (ADR-0012) fuses vector + full-text ranks, so
            # scores run lower than raw cosine — 0.3 (the default) is the
            # calibrated cutoff; 0.6 filtered out every hit.
            "similarity_threshold": 0.3,
        },
        token=api_key,
    )
    print("   Linked Catalog KB → auto mode (top_k=3, threshold=0.3)")

    # Troubleshooting — tool mode (LLM decides when to search)
    api(
        "POST",
        f"/v1/agents/{agent_id}/knowledge-bases",
        {
            "knowledge_base_id": troubleshoot_kb_id,
            "mode": "tool",
            "priority": 2,
            "top_k": 5,
            "similarity_threshold": 0.3,
            "tool_description": (
                "Search the troubleshooting guide for solutions to technical issues. "
                "Use this when the customer reports a problem with their widget, "
                "connectivity, performance, syncing, or billing."
            ),
        },
        token=api_key,
    )
    print("   Linked Troubleshooting KB → tool mode")

    # --- Step 7: Test search ---
    print("\n7. Testing knowledge base search...")
    result = api(
        "POST",
        f"/v1/knowledge-bases/{catalog_kb_id}/search",
        {"query": "What is the price of the enterprise widget?", "top_k": 2},
        token=api_key,
    )
    search_results = result["data"]["results"]
    print(f"   Search returned {len(search_results)} results")
    for r in search_results:
        print(f"   - similarity={r['similarity']:.3f}: {r['content'][:80]}...")

    # --- Done ---
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Agent ID: {agent_id}")
    print("\n  Knowledge Bases:")
    print(f"    FAQ (prompt):          {faq_kb_id}")
    print(f"    Catalog (auto):        {catalog_kb_id}")
    print(f"    Troubleshooting (tool): {troubleshoot_kb_id}")
    print("\n  Test via Chat API:")
    print(f"    curl -X POST {args.server_url}/v1/chat \\")
    print(f'      -H "Authorization: Bearer {api_key[:20]}..." \\')
    print('      -H "Content-Type: application/json" \\')
    body = f'{{"agent_id": "{agent_id}", "message": "What products do you offer?"}}'
    print(f"      -d '{body}'")
    print("\n  Try asking:")
    print('    "What are your hours?"           -> FAQ (prompt mode)')
    print('    "Tell me about Widget Pro"       -> Catalog (auto RAG)')
    print('    "My widget won\'t connect"       -> Troubleshooting (tool mode)')
    print(f"\n  API Key (save this): {api_key}")
    print()


if __name__ == "__main__":
    main()
