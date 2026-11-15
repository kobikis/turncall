"""Agent acceptance on the call-init answers outbound and text check (#476).

Through the running app: a call-init server answers an inline agent, and what
the transport does with it is read from outside — the agent a dial runs as,
whether a text gets a reply and a session, and the log lines left behind. A
refusal keeps each transport's answer to "no usable agent": the dial falls
back to its own agent, the text stays unanswered.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from loguru import logger

from tests.harness import HARNESS_AGENT, Script, answer_outbound_call, run_app
from tests.harness.second_instance import STUB_TEXT_REPLY
from tests.integration.limits_support import SCRIPT
from tests.integration.test_outbound_call_init import _bind_to_call_init
from tests.integration.test_text_session_variables import (
    CallInitServer,
    _call_init_number,
    _sms,
)
from turncall.services.llm_text import CompletionResult

pytestmark = pytest.mark.integration

CALLEE = "+15550007778"
XAI_BASE_URL = {"llm": {"provider": "xai", "base_url": "https://gw.example/v1"}}
BROKEN_GRAPH = {
    "name": "billing",
    "steps": [
        {"id": "ask", "kind": "ask", "start": True, "moves": [{"to": "nowhere"}]},
        {"id": "done", "kind": "end"},
    ],
}


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING the app logs, with its extras."""
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The text LLM, stubbed: the system prompt of each turn it answered."""
    seen: list[str] = []

    async def _complete(_config, messages, **_kwargs) -> CompletionResult:
        seen.append(messages[0]["content"])
        return CompletionResult(text=STUB_TEXT_REPLY, total_tokens=1)

    monkeypatch.setattr("turncall.services.sms_chat.complete_text", _complete)
    return seen


async def _dial(app: Any, server: CallInitServer) -> tuple[str, str]:
    """A dial from a call-init number, naming a stored agent. (call id, agent)"""
    stored = await app.create_agent({**HARNESS_AGENT, "system_prompt": "Dial's."})
    _, number_id = await _bind_to_call_init(app, server.url)
    r = await app.api.post(
        "/v1/calls/outbound",
        json={"agent_id": stored, "from_number_id": number_id, "to_number": CALLEE},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"], stored


async def _pick_up_and_hang_up(app: Any) -> None:
    dial = await app.carrier.wait_for("dial")
    call = await answer_outbound_call(app.carrier, app.api, dial)
    await call.wait_for_agent_speech(1)
    await call.hang_up()
    await app.sink.wait_for("call.ended")


async def test_a_dial_whose_inline_agent_breaks_an_xai_rule_runs_the_dials_agent(
    monkeypatch, warnings
):
    answer = {"agent": {**HARNESS_AGENT, **XAI_BASE_URL}}
    async with (
        CallInitServer(answer) as server,
        run_app(
            Script(agent=("Hello there.",)),
            set_env=monkeypatch.setenv,
            set_attr=monkeypatch.setattr,
        ) as app,
    ):
        call_id, stored = await _dial(app, server)
        await _pick_up_and_hang_up(app)
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]
        project_id = app.project_id

    assert read["active_agent_id"] == stored
    assert "dynamic_config" not in read["metadata"]
    [refused] = [line for line in warnings if "agent_refused" in line]
    assert "origin=call_init" in refused
    assert f"project_id={project_id}" in refused
    assert "base_url is not supported for the xai provider" in refused
    assert call_id in refused


async def test_a_dial_whose_inline_agent_has_an_unknown_field_runs_it(
    monkeypatch, warnings
):
    answer = {"agent": {**HARNESS_AGENT, "voice_style": "warm"}}
    async with (
        CallInitServer(answer) as server,
        run_app(
            Script(agent=("Hello there.",)),
            set_env=monkeypatch.setenv,
            set_attr=monkeypatch.setattr,
        ) as app,
    ):
        call_id, _ = await _dial(app, server)
        await _pick_up_and_hang_up(app)
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]

    assert read["active_agent_id"] is None  # the inline agent ran (ADR-0017)
    assert read["status"] == "completed"
    [warned] = [line for line in warnings if "voice_style" in line]
    assert "origin=call_init" in warned


async def test_a_text_whose_inline_agent_has_a_broken_procedure_stays_unanswered(
    monkeypatch, replies, warnings
):
    answer = {"agent": {**HARNESS_AGENT, "procedures": [BROKEN_GRAPH]}}
    async with (
        CallInitServer(answer) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_number(app, server.url)
        r = await _sms(app, number, "SM1", "hi")
        sessions = (await app.api.get("/v1/chat/sessions")).json()["data"]
        project_id = app.project_id

    assert r.status_code == 200
    assert STUB_TEXT_REPLY not in r.text
    assert replies == []
    assert sessions == []
    [refused] = [line for line in warnings if "agent_refused" in line]
    assert f"project_id={project_id}" in refused
    assert "procedures[0] 'billing'" in refused


async def test_a_text_whose_inline_agent_has_an_unknown_field_is_answered(
    monkeypatch, replies, warnings
):
    answer = {"agent": {**HARNESS_AGENT, "voice_style": "warm"}}
    async with (
        CallInitServer(answer) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _call_init_number(app, server.url)
        first = await _sms(app, number, "SM1", "hi")
        second = await _sms(app, number, "SM2", "and?")
        [session] = (await app.api.get("/v1/chat/sessions")).json()["data"]

    assert STUB_TEXT_REPLY in first.text
    assert STUB_TEXT_REPLY in second.text
    assert session["agent_id"] is None
    # Accepted once, when call-init ran; later messages run on the session.
    [warned] = [line for line in warnings if "voice_style" in line]
    assert "origin=call_init" in warned
