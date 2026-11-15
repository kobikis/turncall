"""webrtc_connect input validation (review: manual JSON parse -> 500 on bad
input). Malformed body / agent_id must be a clean 4xx, not an unhandled 500."""

import contextlib
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


def fake_open_call(seen: dict | None = None) -> AsyncMock:
    """`open_call` with no database or Redis: the source's agent, chosen as
    the real one chooses it, and the call opened under a fresh id."""
    from turncall.services.call_opening import OpenedCall

    async def _open(source, *, project_id, session):
        call_id = uuid.uuid4()
        chosen = await source.choose_agent(
            session, project_id=project_id, call_id=call_id
        )
        opened = OpenedCall(
            call_id=call_id,
            project_id=project_id,
            agent=chosen.agent,
            agent_name=chosen.name,
            metadata=chosen.metadata,
            parties=source.parties,
            handles=source.handles,
        )
        if seen is not None:
            seen["opened"] = opened
        return opened

    return AsyncMock(side_effect=_open)


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


@pytest.mark.unit
def test_a_pipeline_that_cannot_start_is_an_error_not_a_dead_answer(app, monkeypatch):
    """pipecat answers the offer even when the connection callback raised, so
    the browser connected to a peer with nothing behind it. The build failure
    has to come back as an error, and the half-made peer has to be closed."""
    from types import SimpleNamespace

    from tests.accepted import accepted
    from turncall.api.v1 import webrtc
    from turncall.services.call_opening import ChosenAgent

    _auth(app)
    resolved = ChosenAgent(accepted(), None)
    monkeypatch.setattr(webrtc, "_resolve_agent", AsyncMock(return_value=resolved))
    seen: dict = {}
    monkeypatch.setattr(webrtc, "open_call", fake_open_call(seen))
    monkeypatch.setattr(webrtc, "get_engine", lambda: None)
    monkeypatch.setattr(webrtc, "create_session_factory", lambda _e: _session_cm)
    monkeypatch.setattr(webrtc, "create_whatsapp_transport", lambda *a, **k: None)
    monkeypatch.setattr(
        webrtc,
        "run_call",
        AsyncMock(side_effect=RuntimeError("Missing credentials")),
    )
    connection = SimpleNamespace(disconnect=AsyncMock())

    async def handle(_request, callback):
        # What pipecat's handler does: log the callback's error, answer anyway.
        with contextlib.suppress(Exception):
            await callback(connection)
        return {"sdp": "v=0", "type": "answer", "pc_id": "pc"}

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", handle)

    r = app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "agent_id": str(uuid.uuid4())},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 500, r.text
    assert r.json()["code"] == "pipeline_start_failed"
    assert "Missing credentials" in r.json()["error"]
    assert str(seen["opened"].call_id) in r.json()["error"]
    connection.disconnect.assert_awaited_once()


class _SessionCM:
    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *_):
        return False


def _session_cm():
    return _SessionCM()


@pytest.mark.unit
def test_a_missing_key_rides_back_as_a_list(app, monkeypatch):
    """So a console can name the key without parsing the sentence."""
    from types import SimpleNamespace

    from tests.accepted import accepted
    from turncall.api.v1 import webrtc
    from turncall.services.call_opening import ChosenAgent
    from turncall.services.credentials import MissingProviderKeyError

    _auth(app)
    resolved = ChosenAgent(accepted(), None)
    monkeypatch.setattr(webrtc, "_resolve_agent", AsyncMock(return_value=resolved))
    seen: dict = {}
    monkeypatch.setattr(webrtc, "open_call", fake_open_call(seen))
    monkeypatch.setattr(webrtc, "get_engine", lambda: None)
    monkeypatch.setattr(webrtc, "create_session_factory", lambda _e: _session_cm)
    monkeypatch.setattr(webrtc, "create_whatsapp_transport", lambda *a, **k: None)
    monkeypatch.setattr(
        webrtc,
        "run_call",
        AsyncMock(side_effect=MissingProviderKeyError(["deepgram"])),
    )

    async def handle(_request, callback):
        with contextlib.suppress(Exception):
            await callback(SimpleNamespace(disconnect=AsyncMock()))
        return {"sdp": "v=0", "type": "answer", "pc_id": "pc"}

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", handle)
    r = app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "agent_id": str(uuid.uuid4())},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 500
    assert r.json()["details"] == {"missing_keys": ["deepgram"]}
    assert "No API key for deepgram" in r.json()["error"]
