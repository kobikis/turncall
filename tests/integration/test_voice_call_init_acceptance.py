"""Agent acceptance on the call-init answers voice calls check (#477).

Through the running app, a call-init server answers an inline agent on every
voice transport that runs call-init: inbound phone calls on each carrier,
WebRTC connect and WhatsApp voice. An agent breaking a provider rule is
refused at call-init — before the caller is answered and before a call row is
written — the way each transport refuses an answer naming no agent: at the
carrier, with an error response that now says why (WebRTC), or by dropping the
call (WhatsApp voice). Each frees the live-call slot. An agent with a field the
config does not have runs, the field warned about, and its raw config is what
the call row keeps (ADR-0017).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from loguru import logger

from tests.harness import HARNESS_AGENT, run_app
from tests.integration.test_call_init_project_scope import (
    CARRIERS,
    SCRIPT,
    _calls,
    _connect,
    _live_calls,
    _ring_answered,
    _ring_refused,
    _route,
    _whatsapp_number,
    _wrap_voice_llm,
    no_peer,  # noqa: F401 - a fixture
    prompts,  # noqa: F401 - a fixture
    whatsapp_voice,  # noqa: F401 - a fixture
)
from tests.integration.test_live_call_limit import _whatsapp_connect
from tests.integration.test_text_session_variables import CallInitServer

pytestmark = pytest.mark.integration

XAI_REASON = "base_url is not supported for the xai provider"
BROKEN = {
    **HARNESS_AGENT,
    "system_prompt": "Never runs.",
    "llm": {"provider": "xai", "base_url": "https://gw.example/v1"},
}
UNKNOWN_FIELD = {**HARNESS_AGENT, "system_prompt": "Runs.", "voice_style": "warm"}


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING the app logs, with its extras."""
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m)), level="WARNING", format="{message} {extra}"
    )
    yield lines
    logger.remove(sink)


def _refused_line(warnings: list[str], project_id: str) -> str:
    [refused] = [line for line in warnings if "agent_refused" in line]
    assert "origin=call_init" in refused
    assert f"project_id={project_id}" in refused
    assert XAI_REASON in refused
    return refused


def _unknown_field_line(warnings: list[str]) -> str:
    [warned] = [line for line in warnings if "agent_unknown_field" in line]
    assert "origin=call_init" in warned
    assert "voice_style" in warned
    return warned


async def _stored_metadata(call_id: str) -> dict[str, Any]:
    """The call row's metadata as stored, `dynamic_config` included: the call
    read never shows it."""
    from turncall.storage.database import create_session_factory, get_engine
    from turncall.storage.repositories import call_repo

    async with create_session_factory(get_engine())() as session:
        row = await call_repo.get_call_by_id(session, UUID(call_id))
    assert row is not None
    return dict(row.metadata_json)


# -- inbound phone, every carrier --------------------------------------------


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_inbound_refuses_an_inline_agent_breaking_a_provider_rule(
    carrier: str,
    monkeypatch,
    redis_env,
    prompts,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": BROKEN}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        built = _wrap_voice_llm(monkeypatch, prompts)
        number = await _route(app, carrier, server.url)
        await _ring_refused(app, carrier, number)
        calls = await _calls(app)
        slots = await _live_calls(app.project_id)
        project_id = app.project_id

    assert len(server.requests) == 1
    assert calls == []
    assert built == []  # no pipeline was built
    assert slots == 0
    _refused_line(warnings, project_id)


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_inbound_runs_an_inline_agent_with_an_unknown_field(
    carrier: str,
    monkeypatch,
    prompts,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": UNKNOWN_FIELD}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        seen = _wrap_voice_llm(monkeypatch, prompts)
        number = await _route(app, carrier, server.url)
        call = await _ring_answered(app, carrier, number)
        await call.wait_for_agent_speech(1)
        started = await app.sink.wait_for("call.started")
        await call.hang_up()
        await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{started['call_id']}")).json()["data"]
        stored = await _stored_metadata(started["call_id"])

    assert seen == ["Runs."]
    assert read["active_agent_id"] is None  # ADR-0017
    assert stored["dynamic_config"] == UNKNOWN_FIELD  # raw, the field kept
    _unknown_field_line(warnings)


# -- WebRTC ------------------------------------------------------------------


async def test_webrtc_refuses_an_inline_agent_with_the_reason_and_field(
    monkeypatch,
    redis_env,
    no_peer,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": BROKEN}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        r = await _connect(app, server.url)
        calls = await _calls(app)
        slots = await _live_calls(app.project_id)
        project_id = app.project_id

    assert r.status_code == 502, r.text
    body = r.json()
    assert body["success"] is False
    assert body["code"] == "invalid_agent_config"
    assert XAI_REASON in body["error"]
    assert body["details"] == {"reason": XAI_REASON, "field": "llm"}
    assert calls == []
    assert slots == 0
    _refused_line(warnings, project_id)


async def test_webrtc_runs_an_inline_agent_with_an_unknown_field(
    monkeypatch,
    no_peer,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": UNKNOWN_FIELD}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        await _connect(app, server.url)
        started = await app.sink.wait_for("call.started")
        stored = await _stored_metadata(started["call_id"])

    assert started["agent_id"] is None  # ADR-0017
    assert stored["dynamic_config"] == UNKNOWN_FIELD
    _unknown_field_line(warnings)


# -- WhatsApp voice ----------------------------------------------------------


async def test_whatsapp_voice_refuses_an_inline_agent_breaking_a_provider_rule(
    monkeypatch,
    redis_env,
    whatsapp_voice,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": BROKEN}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _whatsapp_number(app, server.url)
        r = await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        slots = await _live_calls(app.project_id)
        calls = await _calls(app)
        project_id = app.project_id

    assert r.status_code == 200, r.text
    assert len(server.requests) == 1
    assert whatsapp_voice == []  # no pipeline started
    assert slots == 0
    assert calls == []
    _refused_line(warnings, project_id)


async def test_whatsapp_voice_runs_an_inline_agent_with_an_unknown_field(
    monkeypatch,
    redis_env,
    whatsapp_voice,  # noqa: F811 - the imported fixture
    warnings,
) -> None:
    async with (
        CallInitServer({"agent": UNKNOWN_FIELD}) as server,
        run_app(
            SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _whatsapp_number(app, server.url)
        await app.api.post("/webhooks/whatsapp", json=_whatsapp_connect(number))
        [(config, context)] = whatsapp_voice
        stored = await _stored_metadata(str(context.call_id))
        read = (await app.api.get(f"/v1/calls/{context.call_id}")).json()["data"]

    assert config.system_prompt == "Runs."
    assert read["active_agent_id"] is None  # ADR-0017
    assert stored["dynamic_config"] == UNKNOWN_FIELD
    _unknown_field_line(warnings)
