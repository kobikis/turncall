"""`variables` on POST /v1/webrtc/connect: a browser call's `{{variables}}`.

A test call from a console has no call-init of its own to send them, and a
WebRTC client may want to set them per call. The request's win a clash with
call-init's, as an outbound dial's do; call-init is shown them.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.accepted import accepted
from tests.unit.test_webrtc_validation import _auth, _session_cm, fake_open_call
from turncall.services.call_opening import CallMetadata, ChosenAgent


class FakeConnection:
    """What the endpoint touches on pipecat's connection."""

    pc_id = "pc"

    def __init__(self) -> None:
        self.disconnect = AsyncMock()
        self.closed: list = []

    def event_handler(self, name: str):
        def register(handler):
            if name == "closed":
                self.closed.append(handler)
            return handler

        return register


def _chosen(config, **metadata) -> ChosenAgent:
    return ChosenAgent(config, None, CallMetadata(**metadata))


def _connect(app, monkeypatch, resolved) -> dict:
    """POST a connect with variables; what the pipeline and call row got."""
    from turncall.api.v1 import webrtc

    _auth(app)
    seen: dict = {}
    monkeypatch.setattr(webrtc, "_resolve_agent", AsyncMock(return_value=resolved))
    monkeypatch.setattr(webrtc, "open_call", fake_open_call(seen))
    monkeypatch.setattr(webrtc, "get_engine", lambda: None)
    monkeypatch.setattr(webrtc, "create_session_factory", lambda _e: _session_cm)
    monkeypatch.setattr(webrtc, "create_whatsapp_transport", lambda *a, **k: None)

    async def _start(opened, *_args, **_kwargs):
        seen["prompt"] = opened.agent.config.system_prompt

    monkeypatch.setattr(webrtc, "run_call", _start)

    async def handle(_request, callback):
        seen["connection"] = FakeConnection()
        await callback(seen["connection"])
        return {"sdp": "v=0", "type": "answer", "pc_id": "pc"}

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", handle)
    r = app.post(
        "/v1/webrtc/connect",
        json={
            "sdp": "v=0",
            "type": "offer",
            "requestData": {
                "agent_id": str(uuid.uuid4()),
                "variables": {"name": "Ada", "vip": True},
            },
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    seen["resolved_with"] = webrtc._resolve_agent.await_args.kwargs["call_id"]
    seen["call_id"] = seen["opened"].call_id
    # What the call row is written with.
    seen["metadata"] = seen["opened"].metadata.row_json()
    return seen


@pytest.mark.unit
def test_the_requests_variables_render_the_agent(app, monkeypatch):
    config = accepted({"system_prompt": "Hi {{name}}, vip={{vip}}."})
    seen = _connect(app, monkeypatch, _chosen(config))

    assert seen["prompt"] == "Hi Ada, vip=True."
    # On the call row, where a handoff and post-call work read them.
    assert seen["metadata"]["template_variables"] == {"name": "Ada", "vip": "True"}


@pytest.mark.unit
def test_call_init_is_asked_with_the_id_the_call_gets(app, monkeypatch):
    config = accepted({"system_prompt": "Hi."})
    seen = _connect(app, monkeypatch, _chosen(config, dynamic_config={"x": 1}))

    assert seen["resolved_with"] == seen["call_id"]


@pytest.mark.unit
def test_they_win_a_clash_with_call_inits(app, monkeypatch):
    config = accepted({"system_prompt": "{{name}} calls from {{caller_number}}."})
    from_call_init = {"name": "Bob", "caller_number": "+1555"}
    seen = _connect(
        app,
        monkeypatch,
        _chosen(config, dynamic_config={"x": 1}, template_variables=from_call_init),
    )

    assert seen["prompt"] == "Ada calls from +1555."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_init_is_shown_them(monkeypatch):
    from turncall.api.v1 import webrtc
    from turncall.services.call_init_resolver import CallInitResult

    sent = AsyncMock(return_value=SimpleNamespace(success=True, data={"agent": {}}))
    monkeypatch.setattr("turncall.events.server_events.send_call_init", sent)
    monkeypatch.setattr(
        "turncall.services.call_init_resolver.resolve_call_init",
        AsyncMock(return_value=CallInitResult(dynamic_config={"system_prompt": "x"})),
    )

    call_id = uuid.uuid4()
    await webrtc._resolve_agent(
        AsyncMock(),
        SimpleNamespace(project_id=uuid.uuid4()),
        None,
        "https://init.example",
        "s",
        {"name": "Ada"},
        call_id=call_id,
    )

    assert sent.await_args.kwargs["variables"] == {"name": "Ada"}
    # The id the call is then created with, not a stand-in.
    assert sent.await_args.kwargs["call_id"] == str(call_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "variables",
    [["name"], {"name": {"first": "Ada"}}, {f"v{n}": "x" for n in range(51)}],
)
def test_variables_that_are_not_a_flat_bounded_object_are_400(app, variables):
    _auth(app)
    r = app.post(
        "/v1/webrtc/connect",
        json={
            "sdp": "v=0",
            "type": "offer",
            "agent_id": str(uuid.uuid4()),
            "variables": variables,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400, r.text
