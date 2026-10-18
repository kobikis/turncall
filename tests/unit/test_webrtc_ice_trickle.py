"""ICE trickle (`PATCH /v1/webrtc/connect`) is authorised as its connection
was (#342).

It used to take no key and check nothing but the `pc_id`, which is the
client's to repeat, not a credential: whoever learned a live call's `pc_id`
could add candidates to its peer connection, whatever project the call was in.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from turncall.domain.enums import ProjectRole

KEY = {"Authorization": "Bearer x"}


def _trickle(pc_id: str) -> dict:
    return {
        "pc_id": pc_id,
        "candidates": [
            {"candidate": "candidate:1 1 udp 1 10.0.0.1 9 typ host", "sdpMid": "0"}
        ],
    }


def _as(app, project_id, role=ProjectRole.DEVELOPER):
    from turncall.api.deps import get_session
    from turncall.auth.context import AuthContext
    from turncall.auth.dependencies import resolve_auth_context

    ctx = AuthContext(project_id=project_id, api_key_id=uuid.uuid4(), role=role)
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()


@pytest.fixture
def held(monkeypatch):
    """A connection this process holds for a live call, and pipecat's handler
    for its candidates."""
    from turncall.api.v1 import webrtc

    pc_id, project_id = "pc-1", uuid.uuid4()
    calls_by_pc = {pc_id: (uuid.uuid4(), project_id)}
    monkeypatch.setattr(webrtc, "_calls_by_pc", calls_by_pc)
    patched = AsyncMock()
    monkeypatch.setattr(webrtc._request_handler, "handle_patch_request", patched)
    return pc_id, project_id, patched


@pytest.mark.unit
def test_the_calls_project_trickles_candidates(app, held):
    pc_id, project_id, patched = held
    _as(app, project_id)  # a developer key, as opens the connection

    r = app.patch("/v1/webrtc/connect", json=_trickle(pc_id), headers=KEY)

    assert r.status_code == 200, r.text
    patched.assert_awaited_once()
    (request,) = patched.await_args.args
    assert request.pc_id == pc_id
    assert [c.sdp_mid for c in request.candidates] == ["0"]


@pytest.mark.unit
def test_no_key_is_401(app, held):
    from turncall.api.deps import get_session

    pc_id, _, patched = held
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()

    r = app.patch("/v1/webrtc/connect", json=_trickle(pc_id))

    assert r.status_code == 401, r.text
    patched.assert_not_awaited()


@pytest.mark.unit
def test_another_projects_connection_and_an_unknown_one_look_the_same(app, held):
    pc_id, _, patched = held
    _as(app, uuid.uuid4())

    theirs = app.patch("/v1/webrtc/connect", json=_trickle(pc_id), headers=KEY)
    unknown = app.patch("/v1/webrtc/connect", json=_trickle("pc-unknown"), headers=KEY)

    assert theirs.status_code == unknown.status_code == 404

    def scrubbed(r, named):
        body = {k: v for k, v in r.json().items() if k != "request_id"}
        return json.dumps(body, sort_keys=True).replace(named, "X")

    # The same answer either way: nothing says the first pc_id is live.
    assert scrubbed(theirs, pc_id) == scrubbed(unknown, "pc-unknown")
    patched.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'"pc-1"'], ids=["not-json", "array", "string"]
)
def test_a_body_that_is_not_a_json_object_is_400(app, held, body):
    _, project_id, patched = held
    _as(app, project_id)

    r = app.patch(
        "/v1/webrtc/connect",
        content=body,
        headers={**KEY, "Content-Type": "application/json"},
    )

    assert r.status_code == 400, r.text
    patched.assert_not_awaited()


@pytest.mark.unit
def test_a_restarted_connection_trickles_under_its_new_pc_id(app, held, monkeypatch):
    """`restart_pc` replaces the peer connection, and pipecat answers with the
    new one's pc_id, which the browser trickles under from then on."""
    from turncall.api.v1 import webrtc

    pc_id, project_id, patched = held
    monkeypatch.setitem(webrtc._request_handler._pcs_map, pc_id, object())
    monkeypatch.setattr(
        webrtc._request_handler,
        "handle_web_request",
        AsyncMock(return_value={"sdp": "v=0", "type": "answer", "pc_id": "pc-2"}),
    )
    _as(app, project_id)

    restarted = app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "pc_id": pc_id, "restart_pc": True},
        headers=KEY,
    )
    new = app.patch("/v1/webrtc/connect", json=_trickle("pc-2"), headers=KEY)
    old = app.patch("/v1/webrtc/connect", json=_trickle(pc_id), headers=KEY)

    assert restarted.status_code == 200, restarted.text
    assert new.status_code == 200, new.text
    assert old.status_code == 404, old.text
    patched.assert_awaited_once()
