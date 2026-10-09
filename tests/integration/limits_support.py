"""Shared by the limits tests (#224-#226): a limited project on the harness.

`redis_env` (in this package's conftest) points the app at a Redis database of
its own and skips without one: the limits are counted there.
"""

from __future__ import annotations

import secrets
from typing import Any

from tests.harness import HARNESS_AGENT, Script, ToolCall

REDIS_URL = "redis://localhost:6379/13"
SCRIPT = Script(
    caller=("Please end the call.",),
    agent=(ToolCall("end_call", {"reason": "caller asked"}), "Goodbye!"),
)
AGENT = {
    **HARNESS_AGENT,
    "tools": [
        {
            "name": "end_call",
            "description": "End the call",
            "parameters_schema": {"type": "object", "properties": {}},
        }
    ],
}


def ring(carrier: Any, to_number: str) -> dict[str, str]:
    """The form Twilio posts to a number's voice URL for a new inbound call."""
    return {
        "AccountSid": carrier.account_sid,
        "CallSid": "CA" + secrets.token_hex(16),
        "CallStatus": "ringing",
        "Direction": "inbound",
        "From": "+15550009999",
        "To": to_number,
    }


async def limited_project(app: Any, **limits: int) -> tuple[str, str, str]:
    """An agent on a bound number, and the project's `limits` set through the
    API. Returns (agent_id, number, number_id)."""
    agent_id = await app.create_agent(AGENT)
    number = await app.bind_number(agent_id)
    numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
    number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
    r = await app.api.put(f"/v1/projects/{app.project_id}/limits", json=limits)
    assert r.status_code == 200, r.text
    assert limits.items() <= r.json()["data"]["limits"].items()
    return agent_id, number, number_id


def dial_body(agent_id: str, number_id: str) -> dict[str, str]:
    return {
        "agent_id": agent_id,
        "from_number_id": number_id,
        "to_number": "+15550007777",
    }
