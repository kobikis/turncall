"""A production WebRTC server_url must be HTTPS — or a Service inside the cluster,
which is where the builder's Agent Backends answer call-init on Kubernetes."""

import pytest

from turncall.api.v1.webrtc import _https_or_in_cluster


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
    assert _https_or_in_cluster(url) is allowed
