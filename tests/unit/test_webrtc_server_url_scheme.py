"""A WebRTC server_url must be HTTPS wherever plain http is not allowed (#340),
or a Service inside the cluster, which is where the builder's Agent Backends
answer call-init on Kubernetes. It is read from requestData, not a pydantic
body, so a refusal is a 400."""

import uuid
from unittest.mock import AsyncMock

import pytest

from turncall.api.deps import get_session
from turncall.api.v1 import webrtc
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.config.settings import Settings
from turncall.domain.enums import ProjectRole
from turncall.services.outbound_url import is_https_or_in_cluster


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://crm.example.com/call-init", True),
        ("http://turncall-agent-9001.turncall.svc.cluster.local:8000/call-init", True),
        ("http://turncall-agent-9001.turncall.svc:8000/call-init", True),
        ("http://crm.example.com/call-init", False),
        # the suffix must be the host's, not smuggled into a path or userinfo
        ("http://evil.com/x.svc.cluster.local", False),
        ("http://x.svc.cluster.local@evil.com/", False),
        ("ftp://a.svc.cluster.local/", False),
    ],
)
def test_production_server_url_scheme(url, allowed):
    assert is_https_or_in_cluster(url) is allowed


def _connect(app, monkeypatch, environment: str, server_url: object):
    monkeypatch.delenv("ALLOW_PLAIN_HTTP_URLS", raising=False)
    monkeypatch.setenv("TURNCALL_ENV", environment)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(webrtc, "get_settings", lambda: settings)
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    return app.post(
        "/v1/webrtc/connect",
        json={"sdp": "v=0", "type": "offer", "requestData": {"server_url": server_url}},
        headers={"Authorization": "Bearer x"},
    )


@pytest.mark.unit
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_plain_http_server_url_is_a_400_outside_development(
    app, monkeypatch, environment
):
    r = _connect(app, monkeypatch, environment, "http://crm.example.com/call-init")

    assert r.status_code == 400, r.text
    assert "server_url must use https://" in r.text
    assert "ALLOW_PLAIN_HTTP_URLS" in r.text


@pytest.mark.unit
def test_a_server_url_that_is_not_a_string_is_a_400(app, monkeypatch):
    r = _connect(app, monkeypatch, "production", 42)

    assert r.status_code == 400, r.text
    assert "server_url must be a string" in r.text
