"""A call-init inline agent's procedures are held to the graph rule (#444)
at call-init (#477). Such an agent skips the agent schema, so a broken graph —
or an entry that is not a procedure at all — reaches agent acceptance, which
refuses it before the caller is answered: the call is refused at the carrier,
no call row is written, and the refusal is logged naming the problem. A bad
call-init answer is visible, never a call running without its procedure.
Seam A: the real app, stub providers, a fake carrier.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from loguru import logger

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.test_call_init_project_scope import (
    _calls,
    _ring_refused,
    _route,
)
from tests.integration.test_harness_call_procedures import NO_INTERNET
from tests.integration.test_text_session_variables import CallInitServer

pytestmark = pytest.mark.integration


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


async def _refused(monkeypatch: Any, warnings: list[str], procedure: Any) -> str:
    """The refusal logged for a call whose inline agent carries `procedure`,
    once the call was refused at the carrier with no call row."""
    inline = {**HARNESS_AGENT, "first_message": "Hello.", "procedures": [procedure]}
    async with (
        CallInitServer({"agent": inline}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _route(app, "twilio", server.url)
        await _ring_refused(app, "twilio", number)
        calls = await _calls(app)

    assert calls == []
    [refused] = [line for line in warnings if "agent_refused" in line]
    assert "origin=call_init" in refused
    return refused


async def test_a_broken_graph_is_refused_naming_the_step(monkeypatch, warnings):
    broken = {**NO_INTERNET, "steps": [dict(s) for s in NO_INTERNET["steps"]]}
    broken["steps"][3]["moves"] = [{"to": "resolve", "when": "the light is green"}]

    refused = await _refused(monkeypatch, warnings, broken)

    assert (
        "procedures[0] 'no_internet': step 'after_restart' moves to 'resolve', "
        "which does not exist" in refused
    ), refused


async def test_an_entry_that_is_not_a_procedure_is_refused(monkeypatch, warnings):
    refused = await _refused(
        monkeypatch, warnings, {"name": "no_internet", "steps": "lights, then restart"}
    )

    assert "procedures[0] 'no_internet': cannot be read: steps" in refused, refused
