"""A live-call limit is a guardrail: Redis being down must not take the line
down (#224).

That every call takes a slot before its row is written and holds it while it
runs is the call module's (`tests/integration/test_call_opening.py`,
`test_run_call.py`); the limit itself is `test_live_call_limit.py`'s.
"""

import uuid

import pytest

from turncall.services import limits

pytestmark = pytest.mark.unit


async def test_without_redis_a_call_is_admitted(monkeypatch):
    def _down():
        raise RuntimeError("Redis not initialized")

    monkeypatch.setattr(limits, "get_redis", _down)
    assert await limits.claim_live_call(uuid.uuid4(), uuid.uuid4(), 1)
    await limits.release_live_call(uuid.uuid4(), uuid.uuid4())  # never raises
