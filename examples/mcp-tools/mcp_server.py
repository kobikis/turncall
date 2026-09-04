"""Example MCP server — CRM tools for TurnCall agents.

Exposes two tools via MCP (Model Context Protocol):
  - lookup_customer: Look up customer by phone number
  - create_ticket: Create a support ticket

Run:
    # Streamable HTTP (recommended)
    python mcp_server.py

    # Or via stdio (for local usage)
    python mcp_server.py --stdio
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("CRM Tools", host="0.0.0.0", port=9002)

# --- Mock Data ---

CUSTOMERS: dict[str, dict] = {
    "+15551112222": {
        "name": "Jane Doe",
        "account_id": "ACC-12345",
        "tier": "enterprise",
        "balance": "$0.00",
        "email": "jane@example.com",
    },
    "+15553334444": {
        "name": "Bob Smith",
        "account_id": "ACC-67890",
        "tier": "starter",
        "balance": "$142.50",
        "email": "bob@example.com",
    },
}

TICKETS: list[dict] = []


# --- MCP Tools ---


@mcp.tool()
def lookup_customer(phone_number: str) -> str:
    """Look up a customer by their phone number.

    Args:
        phone_number: Customer phone number in E.164 format (e.g. +15551112222)
    """
    customer = CUSTOMERS.get(phone_number)
    if customer:
        return json.dumps({"found": True, **customer})
    return json.dumps(
        {"found": False, "message": f"No customer found for {phone_number}"}
    )


@mcp.tool()
def create_ticket(subject: str, description: str, priority: str = "medium") -> str:
    """Create a support ticket for the customer.

    Args:
        subject: Brief ticket subject
        description: Detailed description of the issue
        priority: Ticket priority (low, medium, high)
    """
    ticket_id = f"TKT-{uuid4().hex[:6].upper()}"
    ticket = {
        "id": ticket_id,
        "subject": subject,
        "description": description,
        "priority": priority,
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
    }
    TICKETS.append(ticket)
    return json.dumps(
        {
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Ticket {ticket_id} created.",
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode")
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
