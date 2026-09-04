"""webrtc_connect input validation (review: manual JSON parse -> 500 on bad
input). Malformed body / agent_id must be a clean 4xx, not an unhandled 500."""

import uuid
from unittest.mock import AsyncMock

import pytest

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole


def _auth(app):
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()


@pytest.mark.unit
def test_non_json_body_is_400(app):
    _auth(app)
    r = app.post(
        "/v1/webrtc/connect",
        content=b"not json",
        headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.status_code != 500


@pytest.mark.unit
def test_malformed_agent_id_is_400_not_500(app):
    _auth(app)
    r = app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "requestData": {"agent_id": "not-a-uuid"}},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400, r.text
    assert "UUID" in r.text
    assert r.status_code != 500


@pytest.mark.unit
def test_top_level_agent_id_not_rejected_by_from_dict(app):
    """Extra top-level keys (agent_id) must be stripped before SmallWebRTCRequest,
    not bubble up as an 'unexpected keyword argument' error."""
    _auth(app)
    r = app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "agent_id": "not-a-uuid"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400, r.text
    # Got past signaling parse to agent_id validation
    assert "UUID" in r.text
    assert "unexpected keyword" not in r.text


@pytest.mark.unit
def test_unexpected_signaling_payload_is_400(app):
    _auth(app)
    r = app.post(
        "/v1/webrtc/connect",
        json={"totally": "wrong", "agent_id": "x"},  # from_dict rejects the shape
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400
    assert r.status_code != 500
