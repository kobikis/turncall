"""Opening a voice call, through the module's own interface (#490, seam 1).

`open_call` takes what an edge resolved — here a number and the call's
parties — and answers an opened call or a refusal. What each case checks is
what the world holds afterwards: the call row, its events, and whether the
project's live-call slot is still taken.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.test_text_session_variables import CallInitServer
from turncall.domain.carriers import TWILIO
from turncall.services import call_opening
from turncall.services.call_opening import (
    CallParties,
    CallRefusal,
    EdgeHandles,
    NumberSource,
    OpenedCall,
    RefusalReason,
    open_call,
)
from turncall.storage.database import get_session_factory
from turncall.storage.repositories import call_repo, phone_number_repo

pytestmark = pytest.mark.integration

CALLER = "+15550009999"
INLINE = {**HARNESS_AGENT, "system_prompt": "An inline agent."}


async def _held(project_id: UUID) -> set[str]:
    """The calls holding one of the project's live-call slots."""
    from turncall.services import limits

    members = await limits.get_redis().zrange(limits._live_calls_key(project_id), 0, -1)
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def _open(e164: str, **handles: Any) -> OpenedCall | CallRefusal:
    async with get_session_factory()() as session:
        number = await phone_number_repo.get_by_e164(session, e164)
        assert number is not None
        source = NumberSource(
            number,
            CallParties(from_number=CALLER, to_number=e164),
            EdgeHandles(carrier=TWILIO, provider_call_sid=f"CA{uuid.uuid4().hex}"),
        )
        return await open_call(source, project_id=number.project_id, session=session)


async def _stored(call_id: UUID) -> Any:
    async with get_session_factory()() as session:
        return await call_repo.get_call_by_id(session, call_id)


async def _event_types(app: Any, call_id: UUID) -> list[str]:
    r = await app.api.get(f"/v1/calls/{call_id}/events")
    assert r.status_code == 200, r.text
    return [e["event_type"] for e in r.json()["data"]]


async def _call_init_number(app: Any, server_url: str) -> str:
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": app.carrier.add_number(e164).sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
        },
    )
    assert r.status_code == 201, r.text
    return e164


async def test_a_stored_agents_call_names_it(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        opened = await _open(await app.bind_number(agent_id))
        assert isinstance(opened, OpenedCall), opened
        row = await _stored(opened.call_id)
        events = await _event_types(app, opened.call_id)
        held = await _held(opened.project_id)

    assert str(row.active_agent_id) == agent_id
    assert str(opened.agent.agent_id) == agent_id
    assert "dynamic_config" not in row.metadata_json
    assert row.provider == TWILIO and row.from_number == CALLER
    assert events[:2] == ["call.initializing", "call.started"]
    assert str(opened.call_id) in held


async def test_an_inline_agents_row_keeps_its_config_and_no_agent_id(
    monkeypatch,
) -> None:
    async with (
        CallInitServer({"agent": INLINE, "variables": {"name": "Jane"}}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        opened = await _open(await _call_init_number(app, server.url))
        assert isinstance(opened, OpenedCall), opened
        row = await _stored(opened.call_id)
        events = await _event_types(app, opened.call_id)

    assert row.active_agent_id is None
    assert opened.agent.agent_id is None
    assert row.metadata_json["dynamic_config"] == INLINE
    assert row.metadata_json["template_variables"] == {"name": "Jane"}
    # Call-init was told the id the call was then created with.
    assert server.requests[0]["call"]["id"] == str(opened.call_id)
    assert events[:2] == ["call.initializing", "call.started"]


async def _refused(app: Any, e164: str) -> tuple[CallRefusal, list, set[str]]:
    refusal = await _open(e164)
    assert isinstance(refusal, CallRefusal), refusal
    calls = (await app.api.get("/v1/calls")).json()["data"]
    return refusal, calls, await _held(UUID(app.project_id))


async def test_a_call_init_naming_no_agent_leaves_nothing(monkeypatch) -> None:
    async with (
        CallInitServer({"variables": {"name": "Jane"}}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        refusal, calls, held = await _refused(
            app, await _call_init_number(app, server.url)
        )

    assert refusal.reason is RefusalReason.NO_AGENT
    assert not refusal.busy
    assert (calls, held) == ([], set())


async def test_an_agent_acceptance_refuses_leaves_nothing(monkeypatch) -> None:
    # xAI's text path posts to xAI only: a base_url is refused (#481).
    refused_agent = {
        **HARNESS_AGENT,
        "llm": {"provider": "xai", "base_url": "https://gateway.example.com/v1"},
    }
    async with (
        CallInitServer({"agent": refused_agent}) as server,
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        refusal, calls, held = await _refused(
            app, await _call_init_number(app, server.url)
        )

    assert refusal.reason is RefusalReason.AGENT_REFUSED
    assert (calls, held) == ([], set())


async def test_over_the_live_call_limit_leaves_nothing_but_the_live_call(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(HARNESS_AGENT))
        r = await app.api.put(
            f"/v1/projects/{app.project_id}/limits", json={"live_calls": 1}
        )
        assert r.status_code == 200, r.text
        live = await _open(number)
        assert isinstance(live, OpenedCall)
        refusal, calls, held = await _refused(app, number)

    assert refusal.reason is RefusalReason.LIVE_CALL_LIMIT
    assert refusal.busy
    assert [c["id"] for c in calls] == [str(live.call_id)]
    assert held == {str(live.call_id)}


async def test_a_row_that_cannot_be_written_gives_the_slot_back(monkeypatch) -> None:
    """An inline agent holding a secret with no key to seal it (#338) cannot
    be written: no media stream will ever hold that call's slot or free it."""
    from turncall.storage.sealing import CredentialKeyError

    async def unwritable(*_args: Any, **_kwargs: Any) -> Any:
        raise CredentialKeyError("CREDENTIAL_ENCRYPTION_KEY")

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(HARNESS_AGENT))
        monkeypatch.setattr(call_opening.call_repo, "create_call", unwritable)
        with pytest.raises(CredentialKeyError):
            await _open(number)
        held = await _held(UUID(app.project_id))

    assert held == set()


async def test_the_edges_own_keys_ride_on_the_row(monkeypatch) -> None:
    """Telnyx's Call Control application, read by a transfer, is written with
    the row rather than patched on afterwards."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        e164 = await app.bind_number(await app.create_agent(HARNESS_AGENT))
        async with get_session_factory()() as session:
            number = await phone_number_repo.get_by_e164(session, e164)
            assert number is not None
            opened = await open_call(
                NumberSource(
                    number,
                    CallParties(from_number=CALLER, to_number=e164),
                    EdgeHandles(
                        carrier=TWILIO,
                        provider_call_sid="CA1",
                        row_metadata={"telnyx_connection_id": "conn-1"},
                    ),
                ),
                project_id=number.project_id,
                session=session,
            )
        assert isinstance(opened, OpenedCall)
        row = await _stored(opened.call_id)

    assert row.metadata_json["telnyx_connection_id"] == "conn-1"
    assert row.provider_call_sid == "CA1"
