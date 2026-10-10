"""A renegotiation is the same call, not a new one.

pipecat reuses a peer connection whose `pc_id` it already holds and never
calls back for it. TurnCall used to run call-init, take a live-call slot and
write a call row before handing the offer over — so each renegotiation left a
call stuck at `initiated`, held a slot until its lease lapsed, asked the
customer's call-init server again, and answered with the stray row's id.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_webrtc_validation import _auth


def _offer(pc_id: str | None, **extra) -> dict:
    return {"sdp": "v=0", "type": "offer", "pc_id": pc_id, **extra}


@pytest.fixture
def held(monkeypatch):
    """A connection this process holds, as pipecat keeps it, for a live call."""
    from turncall.api.v1 import webrtc

    pc_id, call_id, project_id = "pc-1", uuid.uuid4(), uuid.uuid4()
    monkeypatch.setitem(webrtc._request_handler._pcs_map, pc_id, object())
    monkeypatch.setitem(webrtc._calls_by_pc, pc_id, (call_id, project_id))
    renegotiated = AsyncMock(
        return_value={"sdp": "v=0", "type": "answer", "pc_id": pc_id}
    )
    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", renegotiated)
    resolve = AsyncMock()
    monkeypatch.setattr(webrtc, "_resolve_agent", resolve)
    admit = AsyncMock()
    monkeypatch.setattr(webrtc, "admit_live_call", admit)
    return SimpleNamespace(
        pc_id=pc_id,
        call_id=call_id,
        project_id=project_id,
        renegotiated=renegotiated,
        resolve=resolve,
        admit=admit,
    )


def _as_project(app, project_id):
    from turncall.api.deps import get_session
    from turncall.auth.context import AuthContext
    from turncall.auth.dependencies import resolve_auth_context
    from turncall.domain.enums import ProjectRole

    ctx = AuthContext(
        project_id=project_id, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()


@pytest.mark.unit
def test_a_renegotiation_answers_under_the_live_calls_id(app, held):
    _as_project(app, held.project_id)
    r = app.post(
        "/v1/webrtc/connect",
        json=_offer(held.pc_id, agent_id=str(uuid.uuid4()), restart_pc=True),
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["call_id"] == str(held.call_id)
    held.renegotiated.assert_awaited_once()
    # Nothing a new call does: no call-init, no slot (and so no row after it).
    held.resolve.assert_not_awaited()
    held.admit.assert_not_awaited()


@pytest.mark.unit
def test_another_projects_connection_cannot_be_renegotiated(app, held):
    _as_project(app, uuid.uuid4())
    r = app.post(
        "/v1/webrtc/connect",
        json=_offer(held.pc_id, agent_id=str(uuid.uuid4())),
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 404, r.text
    held.renegotiated.assert_not_awaited()


@pytest.mark.unit
def test_a_pc_id_this_process_does_not_hold_is_a_new_call(app, held):
    """pipecat opens a new connection for an unknown pc_id, so it is one."""
    from turncall.api.errors import NotFoundError

    _auth(app)
    held.resolve.side_effect = NotFoundError("Agent", "a")  # stop once resolving
    r = app.post(
        "/v1/webrtc/connect",
        json=_offer("pc-unknown", agent_id=str(uuid.uuid4())),
        headers={"Authorization": "Bearer x"},
    )

    assert r.status_code == 404, r.text
    held.resolve.assert_awaited_once()


@pytest.mark.unit
def test_pipecat_still_keeps_its_connections_where_we_look():
    """The renegotiation check reads pipecat's own map, a private attribute:
    a pipecat bump that renames it must fail here, naming why, rather than
    let every renegotiation through as a new call again."""
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequestHandler,
    )

    assert isinstance(SmallWebRTCRequestHandler()._pcs_map, dict)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_started_call_is_held_until_its_connection_closes(app, monkeypatch):
    from tests.unit.test_webrtc_variables import _connect
    from turncall.api.v1 import webrtc
    from turncall.domain.models import AgentConfig

    resolved = (AgentConfig(system_prompt="Hi."), uuid.uuid4(), {}, {}, None, None)
    seen = _connect(app, monkeypatch, resolved)

    assert webrtc._calls_by_pc["pc"][0] == seen["call_id"]
    for closed in seen["connection"].closed:
        await closed(seen["connection"])
    assert "pc" not in webrtc._calls_by_pc
