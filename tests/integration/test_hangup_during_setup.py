"""A caller who hangs up while the call's services connect gets a prompt
`call.ended` (#367).

pipecat 1.13 starts the input transport while later processors are still
setting up, and a pipeline cancelled then sends no frame to a processor not
yet started — the recorder among them. Its status stayed `none`, which
post-call did not count as final, so `call.ended` sat out the whole recording
wait. A slow LLM connect stands in for the provider still connecting.
"""

import asyncio
from typing import Any

import pytest

from tests.harness import (
    HARNESS_AGENT,
    Script,
    place_inbound_call,
    run_app,
)
from tests.harness.stubs import StubLLM
from turncall.services.call_analysis_trigger import RECORDING_WAIT_TIMEOUT_S

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello from the harness."}


async def test_a_hangup_while_connecting_ends_the_call_at_once(monkeypatch) -> None:
    connecting = asyncio.Event()
    setup = StubLLM.setup

    async def _slow_setup(self: StubLLM, *args: Any) -> None:
        await setup(self, *args)
        connecting.set()
        await asyncio.sleep(RECORDING_WAIT_TIMEOUT_S)

    monkeypatch.setattr(StubLLM, "setup", _slow_setup)
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await asyncio.wait_for(connecting.wait(), 5)
        await call.hang_up()
        # Well inside the recording wait it used to sit out.
        ended = (await app.sink.wait_for("call.ended", timeout=5))["payload"]
        await app.post_call_settled()
        sent = [e["event"] for e in app.sink.events if e["call_id"] == call.call_id]

    assert ended["recording_status"] == "none"
    assert sent.count("call.ended") == 1, sent
