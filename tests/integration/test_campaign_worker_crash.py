"""Kill the campaign worker mid-campaign, restart it: no target is lost and
none is dialled twice (#232).

A real worker process (`tests/harness/campaign_worker.py`) dials four targets
and is SIGKILLed on the second, at each moment a dial can be cut short. A fresh
worker then finishes the campaign. The count is the carrier's: what it was
asked, recorded as the request arrived — not what the database remembers.

Where the second target ends up is the janitor's verdict on that moment:
- killed after the claim, before the carrier was asked: it was never dialled,
  so it goes back to `pending` and the new worker dials it — once;
- killed while the carrier had the request, or after it answered but before
  the call row committed: it may have rung, so it ends `failed`
  (`dial_outcome_unknown`) and is never dialled again.
"""

import asyncio
import time
from collections import Counter
from typing import Any

import pytest

from tests.harness import run_app
from tests.harness.campaign_worker import WorkerProcess
from tests.integration.limits_support import SCRIPT, limited_project
from tests.integration.test_campaigns import _create, _end, _numbers, _targets

pytestmark = pytest.mark.integration

_FAST_JANITOR = {
    "CAMPAIGN_CLAIM_TIMEOUT_SECONDS": "1",
    "CAMPAIGN_POLL_SECONDS": "1",
    # Unpaced: this is about crashes, not the calls-per-second floor (#233).
    "CAMPAIGN_DEFAULT_CALLS_PER_SECOND": "0",
}


async def _until(predicate: Any, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition never held")
        await asyncio.sleep(0.2)


@pytest.mark.parametrize(
    ("stall_at", "second_ends"),
    [("claimed", "calling"), ("carrier", "failed"), ("answered", "failed")],
)
async def test_a_killed_worker_loses_no_target_and_dials_none_twice(
    monkeypatch, redis_env, tmp_path, stall_at, second_ends
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id, caller_id, number_id = await limited_project(app)
        numbers = _numbers(4)
        record = tmp_path / "carrier.jsonl"
        worker = WorkerProcess(
            record,
            project_id=app.project_id,
            from_number=caller_id,
            stall_at=stall_at,
            nth=2,
            env=_FAST_JANITOR,
        )
        try:
            campaign_id = await _create(app, agent_id, number_id, numbers)
            await worker.wait_until_stalled()
            worker.kill()
            at_kill = await _targets(app, campaign_id)
        finally:
            worker.stop()

        restarted = WorkerProcess(
            record, project_id=app.project_id, from_number=caller_id, env=_FAST_JANITOR
        )
        try:

            async def _nothing_left_to_dial() -> bool:
                counts = (await app.api.get(f"/v1/campaigns/{campaign_id}")).json()[
                    "data"
                ]["targets"]
                return counts["pending"] + counts["dialling"] + counts["placing"] == 0

            await _until(_nothing_left_to_dial)
            after = await _targets(app, campaign_id)
            # End every call the database knows of; the campaign then closes.
            for dial in restarted.dials():
                if after[dial.params["to_number"]]["status"] == "calling":
                    await _end(app, dial, "ringing", "completed")

            async def _completed() -> bool:
                r = await app.api.get(f"/v1/campaigns/{campaign_id}")
                return r.json()["data"]["status"] == "completed"

            await _until(_completed)
            final = await _targets(app, campaign_id)
        finally:
            restarted.stop()

    first, second, *rest = numbers
    assert at_kill[first]["status"] == "calling"
    assert at_kill[second]["status"] == (
        "dialling" if stall_at == "claimed" else "placing"
    )

    dialled = Counter(d.params["to_number"] for d in restarted.dials())
    assert dialled == Counter(numbers), "a target lost or dialled twice"

    assert after[second]["status"] == second_ends
    if second_ends == "failed":
        assert after[second]["error"].startswith("dial_outcome_unknown")
        assert after[second]["attempts"] == 0
    for number in (first, *rest):
        assert final[number]["status"] == "completed"
        assert final[number]["attempts"] == 1
    if second_ends == "calling":
        assert final[second]["status"] == "completed"
        assert final[second]["attempts"] == 1
