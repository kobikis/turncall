"""Tools Showcase — Webhook Server

Handles:
  1. POST /call-init  — Dynamic assistant resolution (pre-call init)
  2. POST /tools/lookup-customer — CRM lookup by phone number
  3. POST /tools/check-order-status — Order status lookup
  4. POST /tools/create-ticket — Create a support ticket

Run: uvicorn webhook_server:app --port 9000
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="TurnCall Tools Webhook Server")

# --- Mock Data ---

CUSTOMERS: dict[str, dict] = {
    "+15551112222": {
        "name": "Jane Doe",
        "account_id": "ACC-12345",
        "tier": "enterprise",
        "balance": "$0.00",
        "email": "jane@example.com",
        "open_tickets": [
            {"id": "TKT-101", "subject": "Billing dispute", "status": "open"},
            {"id": "TKT-102", "subject": "Feature request", "status": "in_progress"},
        ],
    },
    "+15553334444": {
        "name": "Bob Smith",
        "account_id": "ACC-67890",
        "tier": "starter",
        "balance": "$142.50",
        "email": "bob@example.com",
        "open_tickets": [],
    },
}

ORDERS: dict[str, dict] = {
    "ORD-001": {
        "customer": "ACC-12345",
        "status": "shipped",
        "tracking": "1Z999AA10123456784",
        "items": ["Widget Pro x2", "Cable Kit x1"],
        "estimated_delivery": "2026-04-16",
    },
    "ORD-002": {
        "customer": "ACC-67890",
        "status": "processing",
        "tracking": None,
        "items": ["Starter Pack x1"],
        "estimated_delivery": "2026-04-20",
    },
}

# Store for created tickets (in-memory)
TICKETS: list[dict] = []

import os

# Agent ID — set via: export SUPPORT_AGENT_ID=<uuid>
SUPPORT_AGENT_ID: str = os.environ.get("SUPPORT_AGENT_ID", "")


@app.post("/call-init")
async def call_init(request: Request) -> JSONResponse:
    """Handle TurnCall call-init server event.

    Called before the pipeline starts. Returns:
    - Which agent to use
    - Template variables for prompt personalization
    - Knowledge context (CRM data injected into system prompt)
    - Metadata stored on call record
    """
    body = await request.json()
    message = body.get("message", {})
    customer_number = message.get("customer", {}).get("number", "")
    turncall_number = message.get("phoneNumber", {}).get("number", "")
    call_type = message.get("call", {}).get("type", "unknown")

    print(f"\n{'='*60}")
    print("  Agent REQUEST")
    print(f"  Customer: {customer_number}")
    print(f"  TurnCall Number: {turncall_number}")
    print(f"  Call Type: {call_type}")
    print(f"{'='*60}")

    # Look up customer
    customer = CUSTOMERS.get(customer_number)

    if customer:
        # Known customer — personalize the experience
        open_tickets = customer["open_tickets"]
        ticket_summary = (
            f"They have {len(open_tickets)} open tickets: "
            + ", ".join(f"#{t['id']} ({t['subject']})" for t in open_tickets)
            if open_tickets
            else "They have no open tickets."
        )

        knowledge_context = (
            f"CALLER CONTEXT (from CRM):\n"
            f"- Name: {customer['name']}\n"
            f"- Account: {customer['account_id']}\n"
            f"- Tier: {customer['tier']}\n"
            f"- Balance: {customer['balance']}\n"
            f"- {ticket_summary}\n"
            f"- Greet them by name. Proactively ask if they need help with open tickets."
        )

        print(f"  Known customer: {customer['name']} ({customer['tier']})")

        return JSONResponse(
            {
                "agent_id": SUPPORT_AGENT_ID,
                "variables": {
                    "customer_name": customer["name"],
                    "account_id": customer["account_id"],
                    "tier": customer["tier"],
                },
                "metadata": {
                    "crm_id": customer["account_id"],
                    "segment": customer["tier"],
                    "caller_identified": True,
                },
                "dynamic_data": {
                    "knowledge_context": knowledge_context,
                },
            }
        )

    # Unknown customer — use default assistant without personalization
    print("  Unknown customer — using default config")
    return JSONResponse(
        {
            "agent_id": SUPPORT_AGENT_ID,
            "variables": {
                "customer_name": "valued customer",
                "account_id": "unknown",
                "tier": "unknown",
            },
            "metadata": {
                "caller_identified": False,
            },
        }
    )


@app.post("/tools/lookup-customer")
async def lookup_customer(request: Request) -> JSONResponse:
    """Handle lookup_customer tool webhook call.

    TurnCall POSTs: {tool_name, arguments, call_id, project_id}
    """
    body = await request.json()
    args = body.get("arguments", {})
    phone_number = args.get("phone_number", "")

    print(f"\n  TOOL: lookup_customer | phone={phone_number}")

    customer = CUSTOMERS.get(phone_number)
    if customer:
        return JSONResponse(
            {
                "found": True,
                "customer_name": customer["name"],
                "account_id": customer["account_id"],
                "tier": customer["tier"],
                "balance": customer["balance"],
                "email": customer["email"],
                "open_tickets": len(customer["open_tickets"]),
            }
        )

    return JSONResponse(
        {"found": False, "message": f"No customer found for {phone_number}"}
    )


@app.post("/tools/check-order-status")
async def check_order_status(request: Request) -> JSONResponse:
    """Handle check_order_status tool webhook call."""
    body = await request.json()
    args = body.get("arguments", {})
    order_id = args.get("order_id", "")
    account_id = args.get("account_id", "")

    print(f"\n  TOOL: check_order_status | order={order_id} account={account_id}")

    # Look up by order ID
    if order_id and order_id in ORDERS:
        order = ORDERS[order_id]
        return JSONResponse(
            {
                "found": True,
                "order_id": order_id,
                "status": order["status"],
                "items": order["items"],
                "tracking_number": order["tracking"],
                "estimated_delivery": order["estimated_delivery"],
            }
        )

    # Look up by account ID (return most recent)
    if account_id:
        for oid, order in ORDERS.items():
            if order["customer"] == account_id:
                return JSONResponse(
                    {
                        "found": True,
                        "order_id": oid,
                        "status": order["status"],
                        "items": order["items"],
                        "tracking_number": order["tracking"],
                        "estimated_delivery": order["estimated_delivery"],
                    }
                )

    return JSONResponse({"found": False, "message": "No order found."})


@app.post("/tools/create-ticket")
async def create_ticket(request: Request) -> JSONResponse:
    """Handle create_ticket tool webhook call."""
    body = await request.json()
    args = body.get("arguments", {})
    subject = args.get("subject", "No subject")
    description = args.get("description", "")
    priority = args.get("priority", "medium")
    call_id = body.get("call_id", "")

    ticket_id = f"TKT-{uuid4().hex[:6].upper()}"
    ticket = {
        "id": ticket_id,
        "subject": subject,
        "description": description,
        "priority": priority,
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
        "call_id": call_id,
    }
    TICKETS.append(ticket)

    print(f"\n  TOOL: create_ticket | id={ticket_id} subject='{subject}'")

    return JSONResponse(
        {
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Ticket {ticket_id} created successfully. "
            f"Subject: '{subject}'. Priority: {priority}.",
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Health check."""
    return JSONResponse({"status": "ok", "tickets_created": len(TICKETS)})
