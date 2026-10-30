"""A call-init inline agent's procedures are held to the API's rule at build
(#444). Such an agent skipped the agent schema, so a broken graph — or an
entry that is not a procedure at all — reaches the build, which fails the call
as `pipeline_error` with an `error.raised` naming the problem: a bad call-init
answer is visible, never a call running without its procedure. Seam A: the
real app, stub providers, a fake carrier.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.harness.twilio import place_inbound_call
from tests.integration.test_harness_call_procedures import (
    CALLER,
    NO_INTERNET,
    _bind_to_call_init,
    _CallInit,
)

pytestmark = pytest.mark.integration


async def _failed_build(monkeypatch: Any, procedure: Any) -> tuple[dict, list[str]]:
    """The call.ended and the error.raised texts of a call whose inline
    agent carries `procedure`."""
    inline = {**HARNESS_AGENT, "first_message": "Hello.", "procedures": [procedure]}
    async with (
        _CallInit(inline) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _bind_to_call_init(app, server.url)
        await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        ended = await app.sink.wait_for("call.ended")
        events = (await app.api.get(f"/v1/calls/{ended['call_id']}/events")).json()

    errors = [
        e["payload"]["error"]
        for e in events["data"]
        if e["event_type"] == "error.raised"
    ]
    return ended["payload"], errors


async def test_a_broken_graph_fails_the_call_naming_the_step(monkeypatch):
    broken = {**NO_INTERNET, "steps": [dict(s) for s in NO_INTERNET["steps"]]}
    broken["steps"][3]["moves"] = [{"to": "resolve", "when": "the light is green"}]

    ended, errors = await _failed_build(monkeypatch, broken)

    assert ended["ended_reason"] == "pipeline_error"
    assert any(
        "procedures[0] 'no_internet': step 'after_restart' moves to 'resolve', "
        "which does not exist" in e
        for e in errors
    ), errors


async def test_an_entry_that_is_not_a_procedure_fails_the_call(monkeypatch):
    ended, errors = await _failed_build(
        monkeypatch, {"name": "no_internet", "steps": "lights, then restart"}
    )

    assert ended["ended_reason"] == "pipeline_error"
    assert any(
        "procedures[0] 'no_internet': cannot be read: steps" in e for e in errors
    ), errors
