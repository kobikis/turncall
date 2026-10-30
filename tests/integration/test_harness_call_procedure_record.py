"""What a call records of its procedures (#443), over the running app.

Each accepted move is a `procedure.step` call event, each refused one a
`procedure.refused`, on the call's event log in the order they were made, and
`procedures` on the call read and `call.ended` — one derivation — say which
path each procedure took and where it ended. Seam A: the real app, stub
providers, a fake carrier.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Given, Script, run_app
from tests.integration.test_harness_call_procedures import (
    NO_INTERNET,
    _call,
    _move,
    _procedure_events,
    _remembering,
    _until,
)

pytestmark = pytest.mark.integration


async def test_the_path_taken_is_on_the_event_log_and_in_the_summary(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=(
            "The internet light is red.",
            "Okay, it's back on.",
            "Yes, I waited the two minutes.",
            "It's green now.",
        ),
        agent=(
            _remembering(given, _move("restart")),
            _remembering(given, "Restart it and wait two minutes."),
            # Skips the confirmation: refused.
            _remembering(given, _move("resolved")),
            _remembering(given, "Did you wait the full two minutes?"),
            _remembering(given, _move("after_restart")),
            _remembering(given, "Is the internet light green?"),
            _remembering(given, _move("resolved")),
            _remembering(given, "Glad it's working. Goodbye."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [NO_INTERNET],
                }
            )
        )
        call = await _call(app, number)
        for turn, inferences in enumerate((2, 4, 6, 8), start=2):
            await call.say(400)
            await _until(lambda n=inferences: len(given) >= n)
            await call.wait_for_agent_speech(turn)

        # Written off the tool's path: wait for them.
        events = await _procedure_events(app, call.call_id, at_least=4)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    def step(frm: str, to: str, kind: str) -> tuple[str, dict[str, Any]]:
        payload = {"procedure": "no_internet", "from": frm, "to": to, "kind": kind}
        return ("procedure.step", payload)

    assert events == [
        step("lights", "restart", "instruct"),
        (
            "procedure.refused",
            {"procedure": "no_internet", "from": "restart", "to": "resolved"},
        ),
        step("restart", "after_restart", "check"),
        step("after_restart", "resolved", "end"),
    ]
    expected = [
        {
            "name": "no_internet",
            "path": ["lights", "restart", "after_restart", "resolved"],
            "completed": True,
            "ended_at": "resolved",
            "escalated_at": None,
            "refused_moves": 1,
        }
    ]
    assert ended["payload"]["procedures"] == expected
    assert read["procedures"] == expected


async def test_a_procedure_left_part_way_is_not_completed(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.",),
        agent=(
            _remembering(given, _move("restart")),
            _remembering(given, "Restart it and wait two minutes."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [NO_INTERNET],
                }
            )
        )
        call = await _call(app, number)
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

    [procedure] = ended["payload"]["procedures"]
    assert procedure["path"] == ["lights", "restart"]
    assert procedure["completed"] is False
    assert procedure["ended_at"] == "restart"


async def test_a_call_with_no_procedures_records_none(monkeypatch):
    script = Script(caller=("Hi.",), agent=("Hello, how can I help?",))
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent({**HARNESS_AGENT, "first_message": "Hello."})
        )
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call.call_id}")).json()["data"]

    assert ended["payload"]["procedures"] == []
    assert read["procedures"] == []
