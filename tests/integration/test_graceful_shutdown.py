"""SIGTERM lets a live call finish (#199), against a real server process.

Uvicorn's own SIGTERM handling closes the listening socket and every open
connection at once — a Twilio media stream gets close code 1012 — so before
this, a deploy cut off every call on the replica it stopped. Here the process
is signalled mid-call: readiness has to fail while liveness stays green, the
caller must still be heard and answered, the call must end the way the caller
ended it, `call.ended` must reach the subscriber, and only then does the
process exit.
"""

import asyncio
import signal

import pytest

from tests.harness import HARNESS_AGENT, Script, place_inbound_call
from tests.harness.server import serve_process

pytestmark = pytest.mark.integration

SCRIPT = Script(caller=("Are you still there?",), agent=("Still here.",))
AGENT = {**HARNESS_AGENT, "first_message": "Hello from the harness."}


async def _active_calls(http) -> int:
    body = (await http.get("/metrics")).text
    line = next(x for x in body.splitlines() if x.startswith("turncall_active_calls "))
    return int(line.split()[1])


async def test_a_live_call_survives_sigterm_and_finishes_normally():
    async with serve_process(SCRIPT) as server:
        app = server.harness
        number = await server.bind_number(await app.create_agent(AGENT))
        assert await _active_calls(app.api) == 0
        assert (await app.api.get("/ready")).status_code == 200

        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        assert await _active_calls(app.api) == 1

        server.signal(signal.SIGTERM)
        await asyncio.sleep(0.3)
        ready = await app.api.get("/ready")
        assert ready.status_code == 503, server.log_tail()
        assert ready.json() == {"status": "draining", "active_calls": 1}
        assert (await app.api.get("/live")).status_code == 200
        assert server.process.returncode is None

        # The call goes on: the caller is heard and answered after the signal.
        await call.say(400)
        await call.wait_for_agent_speech(2)
        assert not call.closed.is_set(), "the server closed the stream"
        assert (await app.api.get("/live")).status_code == 200

        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        exit_code = await server.exited(timeout=30)

    payload = ended["payload"]
    assert payload["status"] == "completed"
    assert payload["ended_reason"] == "customer_ended_call"
    said = [(t["role"], t["text"]) for t in payload["transcript"]]
    assert ("customer", "Are you still there?") in said
    assert ("agent", "Still here.") in said
    # Uvicorn re-raises the SIGTERM once it has shut down, as it always does:
    # the process ends by the signal (a container reports 143).
    assert exit_code == -signal.SIGTERM, server.log_tail()


async def test_sigterm_with_no_live_calls_exits_at_once():
    async with serve_process(SCRIPT) as server:
        server.signal(signal.SIGTERM)
        exit_code = await server.exited(timeout=10)
    assert exit_code == -signal.SIGTERM, server.log_tail()
