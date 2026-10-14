"""The demo server's signaling, short of a call (`tests/integration/test_demo_call.py`
holds one end to end)."""

from __future__ import annotations

import httpx
import pytest

from turncall.config.settings import Settings
from turncall.demo.app import create_demo_app
from turncall.domain.models import AgentConfig

pytestmark = pytest.mark.unit

OFFER = {"sdp": "v=0", "type": "offer"}


@pytest.fixture
def http():
    app = create_demo_app(
        {"front-desk": AgentConfig(), "restaurant": AgentConfig()}, Settings()
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://demo"
    )


async def test_it_names_its_agents_the_first_the_default(http) -> None:
    body = (await http.get("/agents")).json()
    assert body == {"agents": ["front-desk", "restaurant"], "default": "front-desk"}


async def test_an_idle_server_is_ready(http) -> None:
    response = await http.get("/ready")
    assert (response.status_code, response.json()["status"]) == (200, "ready")


async def test_an_offer_must_be_a_json_object(http) -> None:
    assert (await http.post("/connect", content=b"[1]")).status_code == 400
    assert (await http.post("/connect", content=b"nope")).status_code == 400


async def test_an_unknown_agent_is_a_404(http) -> None:
    response = await http.post(
        "/connect", json={**OFFER, "requestData": {"demo": "dentist"}}
    )
    assert response.status_code == 404
    assert "dentist" in response.json()["error"]


async def test_a_draining_server_takes_no_call(http, monkeypatch) -> None:
    from turncall.services import drain

    monkeypatch.setattr(drain, "is_draining", lambda: True)
    assert (await http.get("/ready")).json()["status"] == "draining"
    assert (await http.post("/connect", json=OFFER)).status_code == 503
