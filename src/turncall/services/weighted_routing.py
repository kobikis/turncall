"""Weighted A/B routing for phone numbers.

Deterministic: same caller always reaches the same agent variant
(as long as weights don't change).
"""

import hashlib
from uuid import UUID


def pick_agent_by_weight(
    routing_weights: list[dict],
    caller_number: str,
) -> UUID:
    """Select an agent ID from weighted routing config.

    Uses SHA256 hash of caller number for deterministic, stable assignment.
    Same caller always hits the same variant.

    Args:
        routing_weights: List of {"agent_id": "uuid", "weight": int} entries.
                         Weights must sum to 100.
        caller_number: Caller's phone number (E.164) used as hash input.

    Returns:
        The selected agent UUID.
    """
    digest = hashlib.sha256(caller_number.encode()).hexdigest()
    bucket = int(digest[:8], 16) % 100

    cumulative = 0
    for entry in routing_weights:
        cumulative += entry["weight"]
        if bucket < cumulative:
            return UUID(entry["agent_id"])

    # Fallback to last entry (handles rounding edge cases)
    return UUID(routing_weights[-1]["agent_id"])
