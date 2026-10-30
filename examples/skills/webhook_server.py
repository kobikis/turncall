"""Skills example — the tools' webhook server (a mock CRM and billing system).

Each skill's tools POST here: `{tool_name, arguments, project_id, call_id,
session_id}`. Whatever JSON this answers is the tool's result.

Run: uvicorn webhook_server:app --host 0.0.0.0 --port 9100
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request

app = FastAPI(title="Skills example tools")

# last 4 digits of the account -> the customer
CUSTOMERS: dict[str, dict] = {
    "4471": {"name": "Jane Doe", "dob": "1985-03-03", "account_id": "ACC-4471"},
    "2210": {"name": "Sam Lee", "dob": "1990-07-14", "account_id": "ACC-2210"},
}

INVOICES: dict[str, list[dict]] = {
    "ACC-4471": [
        {
            "month": "September 2026",
            "total": 48.00,
            "lines": [{"item": "Unlimited plan", "amount": 48.00}],
            "paid": True,
        },
        {
            "month": "October 2026",
            "total": 70.00,
            "lines": [
                {"item": "Unlimited plan", "amount": 48.00},
                {"item": "Tariff adjustment (from October)", "amount": 12.00},
                {"item": "Late fee (September paid 9 days late)", "amount": 10.00},
            ],
            "paid": False,
            "due_date": "2026-11-15",
            "payment_reference": "88-4471",
        },
    ],
    "ACC-2210": [
        {
            "month": "September 2026",
            "total": 30.00,
            "paid": True,
            "lines": [{"item": "Basic plan", "amount": 30.00}],
        },
        {
            "month": "October 2026",
            "total": 30.00,
            "paid": False,
            "lines": [{"item": "Basic plan", "amount": 30.00}],
            "due_date": "2026-11-15",
            "payment_reference": "88-2210",
        },
    ],
}

PROMISES: list[dict] = []


async def _arguments(request: Request) -> dict:
    body = await request.json()
    print(f"-> {body.get('tool_name')} {body.get('arguments')}")
    return body.get("arguments") or {}


@app.post("/tools/verify-customer")
async def verify_customer(request: Request) -> dict:
    args = await _arguments(request)
    customer = CUSTOMERS.get(str(args.get("last4", "")).strip())
    if customer is None or customer["dob"] != str(args.get("dob", "")).strip():
        return {"verified": False}
    return {
        "verified": True,
        "name": customer["name"],
        "account_id": customer["account_id"],
    }


@app.post("/tools/get-invoices")
async def get_invoices(request: Request) -> dict:
    args = await _arguments(request)
    invoices = INVOICES.get(str(args.get("account_id", "")).strip())
    if invoices is None:
        return {"error": "unknown account"}
    return {"invoices": invoices}


@app.post("/tools/create-payment-promise")
async def create_payment_promise(request: Request) -> dict:
    args = await _arguments(request)
    promise = {
        "promise_id": f"PP-{uuid4().hex[:6].upper()}",
        "account_id": args.get("account_id"),
        "amount": args.get("amount"),
        "pay_by": args.get("pay_by"),
        "late_fees_paused": True,
    }
    PROMISES.append(promise)
    return {"confirmed": True, **promise}
