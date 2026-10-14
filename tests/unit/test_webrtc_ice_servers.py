"""`WEBRTC_ICE_SERVERS`: what our side of a WebRTC call gathers candidates with.

Public STUN unless set. Behind NAT — a Kubernetes pod, a Cloudflare Container,
which takes no inbound UDP at all — the browser connects and hears nothing
without a TURN relay, so the relay has to be configurable.
"""

from __future__ import annotations

import json

import pytest

from turncall.config.settings import DEFAULT_ICE_SERVERS, Settings

pytestmark = pytest.mark.unit

TURN = {
    "urls": "turns:turn.cloudflare.com:443?transport=tcp",
    "username": "u",
    "credential": "c",
}


def test_public_stun_by_default(monkeypatch) -> None:
    monkeypatch.delenv("WEBRTC_ICE_SERVERS", raising=False)
    assert Settings().webrtc.ice_servers == list(DEFAULT_ICE_SERVERS)


def test_a_turn_relay_is_read_as_json(monkeypatch) -> None:
    monkeypatch.setenv("WEBRTC_ICE_SERVERS", json.dumps([TURN]))
    (server,) = Settings().webrtc.ice_servers
    assert (server.urls, server.username, server.credential) == (
        TURN["urls"],
        "u",
        "c",
    )


def test_a_peer_connection_gets_them_in_pipecats_shape(monkeypatch) -> None:
    from pipecat.transports.smallwebrtc.connection import IceServer

    from turncall.config.settings import get_settings
    from turncall.orchestrator.transport_factory import ice_servers

    monkeypatch.setenv(
        "WEBRTC_ICE_SERVERS", json.dumps([TURN, {"urls": "stun:stun.example:3478"}])
    )
    get_settings.cache_clear()
    try:
        servers = ice_servers()
    finally:
        get_settings.cache_clear()
    assert all(isinstance(s, IceServer) for s in servers)
    assert [s.urls for s in servers] == [TURN["urls"], "stun:stun.example:3478"]
    assert servers[0].credential == "c"
