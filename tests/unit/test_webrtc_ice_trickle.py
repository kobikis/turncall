"""ICE trickle (`PATCH /v1/webrtc/connect`) is authorised as its connection
was (#342).

It used to take no key and check nothing but the `pc_id`, which is the
client's to repeat, not a credential: whoever learned a live call's `pc_id`
could add candidates to its peer connection, whatever project the call was in.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_webrtc_renegotiation import _as_project


def _trickle(pc_id: str) -> dict:
    return {
        "pc_id": pc_id,
        "candidates": [
            {"candidate": "candidate:1 1 udp 1 10.0.0.1 9 typ host", "sdpMid": "0"}
        ],
    }


@pytest.fixture
def held(monkeypatch):
    """A connection this process holds for a live call, and pipecat's handler
    for its candidates."""
    from turncall.api.v1 import webrtc

    pc_id, project_id = "pc-1", uuid.uuid4()
    monkeypatch.setitem(webrtc._calls_by_pc, pc_id, (uuid.uuid4(), project_id))
    patched = AsyncMock()
    monkeypatch.setattr(webrtc._request_handler, "handle_patch_request", patched)
    return pc_id, project_id, patched


@pytest.mark.unit
def test_the_calls_project_trickles_candidates(app, held):
    pc_id, project_id, patched = held
    _as_project(app, project_id)

    r = app.patch(
        "/v1/webrtc/connect",
        json=_trickle(pc_id),
        headers={"Authorization": "Bearer x"},
    )

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
    _as_project(app, uuid.uuid4())

    theirs = app.patch(
        "/v1/webrtc/connect",
        json=_trickle(pc_id),
        headers={"Authorization": "Bearer x"},
    )
    unknown = app.patch(
        "/v1/webrtc/connect",
        json=_trickle("pc-unknown"),
        headers={"Authorization": "Bearer x"},
    )

    assert theirs.status_code == unknown.status_code == 404
    # The same answer either way: nothing says the first pc_id is live.
    assert theirs.json()["code"] == unknown.json()["code"]
    assert theirs.json()["error"].replace(pc_id, "X") == unknown.json()[
        "error"
    ].replace("pc-unknown", "X")
    patched.assert_not_awaited()
