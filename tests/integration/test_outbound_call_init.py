"""An outbound call from a webhook-routed number runs call-init (#277).

Through the running app: the number is bound to a call-init server, the call
is dialled through the API, and the callee picks up. The agent that answers is
the inline one the server built for this call — the case of an agent whose MCP
headers carry a token minted per call — not the agent the dial named.
"""

import json
import uuid
from typing import Any

import pytest
from aiohttp import web

from tests.harness import HARNESS_AGENT, Script, answer_outbound_call, run_app

pytestmark = pytest.mark.integration

CALLEE = "+15550007777"


class CallInitServer:
    """A customer's call-init endpoint: records each request, answers an
    inline agent."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> "CallInitServer":
        app = web.Application()
        app.router.add_post("/call-init", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/call-init"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append(json.loads(await request.text())["message"])
        return web.json_response(
            {
                "agent": {
                    **HARNESS_AGENT,
                    "system_prompt": "Built per call for {{name}}.",
                },
                "variables": {"name": "Ada"},
            }
        )


async def _bind_to_call_init(app: Any, server_url: str) -> tuple[str, str]:
    """A number on the fake carrier, routed to `server_url`. (e164, id)"""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
        },
    )
    assert r.status_code == 201, r.text
    return e164, r.json()["data"]["id"]


async def test_an_outbound_call_runs_the_agent_call_init_built(monkeypatch):
    from turncall.orchestrator import pipeline_factory

    prompts: list[str] = []
    async with (
        CallInitServer() as server,
        run_app(
            Script(agent=("Hello there.",)),
            set_env=monkeypatch.setenv,
            set_attr=monkeypatch.setattr,
        ) as app,
    ):
        stub = pipeline_factory._create_llm_service

        def _llm(config: Any, *args: Any, **kwargs: Any) -> Any:
            prompts.append(config.system_prompt)
            return stub(config, *args, **kwargs)

        monkeypatch.setattr(pipeline_factory, "_create_llm_service", _llm)
        stored = await app.create_agent(
            {**HARNESS_AGENT, "system_prompt": "The stored agent."}
        )
        number, number_id = await _bind_to_call_init(app, server.url)
        r = await app.api.post(
            "/v1/calls/outbound",
            json={
                "agent_id": stored,
                "from_number_id": number_id,
                "to_number": CALLEE,
            },
        )
        assert r.status_code == 201, r.text
        call_id = r.json()["data"]["id"]

        dial = await app.carrier.wait_for("dial")
        call = await answer_outbound_call(app.carrier, app.api, dial)
        await call.wait_for_agent_speech(1)
        await call.hang_up()
        await app.sink.wait_for("call.ended")
        read = (await app.api.get(f"/v1/calls/{call_id}")).json()["data"]

    [asked] = server.requests
    assert asked["type"] == "call-init"
    assert asked["call"]["type"] == "outboundPhoneCall"
    assert asked["call"]["id"] == call_id
    assert asked["call"]["agent_id"] == stored
    assert asked["phoneNumber"]["number"] == number
    assert asked["customer"]["number"] == CALLEE

    assert prompts == ["Built per call for Ada."]
    assert read["active_agent_id"] is None  # an inline agent has no row (ADR-0017)
    assert "dynamic_config" not in read["metadata"]  # never shown (#276)
