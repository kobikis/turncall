"""Every transport's call announces itself to subscribers.

A WebRTC call stored `call.initializing` without dispatching it and never
wrote `call.started`; WhatsApp voice stored both without dispatching; an
outbound call stored `call.started` only. Only a carrier's inbound call went
through `emit_call_event`. Seen in the builder console's live feed: a WebRTC
call that began with its first transcript."""

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app

pytestmark = pytest.mark.integration


async def test_a_webrtc_call_dispatches_initializing_then_started(monkeypatch):
    from turncall.api.v1 import webrtc

    async def _no_peer(*_a, **_k):
        # The events are written before the peer is negotiated, and a real
        # aiortc peer left behind would keep the test process alive.
        raise RuntimeError("no peer in this test")

    monkeypatch.setattr(webrtc._request_handler, "handle_web_request", _no_peer)
    async with run_app(
        Script(caller=(), agent=()),
        set_env=monkeypatch.setenv,
        set_attr=monkeypatch.setattr,
    ) as app:
        agent_id = await app.create_agent(HARNESS_AGENT)
        await app.api.post(
            "/v1/webrtc/connect",
            json={"sdp": "v=0", "type": "offer", "agent_id": agent_id},
        )
        started = await app.sink.wait_for("call.started")
        initializing = app.sink.of("call.initializing")

    assert started["call_id"] and started["payload"]["transport"] == "webrtc"
    assert [e["call_id"] for e in initializing] == [started["call_id"]]
