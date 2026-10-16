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


async def test_the_call_path_is_imported_at_startup_not_on_the_first_call(
    monkeypatch,
) -> None:
    """The first offer used to pay the call path's imports — 1.7 s on a demo
    container's vCPU, between the answer and the pipeline. Startup does them,
    off the event loop, so the visitor's own setup time covers them."""
    import asyncio

    from turncall.demo import app as demo_app

    warmed = asyncio.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        demo_app, "import_call_path", lambda: loop.call_soon_threadsafe(warmed.set)
    )
    app = create_demo_app({"front-desk": AgentConfig()}, Settings())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(warmed.wait(), timeout=5)
