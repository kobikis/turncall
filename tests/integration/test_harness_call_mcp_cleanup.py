"""A carrier call closes the MCP sessions it opened, however it ends (#488).

Over the running app, stub providers, a fake carrier, and a fake MCP client
standing in for the agent's servers. A call whose build raises still fails as
`pipeline_error` with `error.raised` and `call.ended`, and neither it nor
one whose build is cancelled before it starts may leave its MCP sessions
open. A call that runs closes them once, at its own cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from uuid import UUID

import pytest

from tests.harness import HARNESS_AGENT, Script, run_app
from tests.integration.test_call_init_project_scope_whatsapp import _live_calls
from tests.integration.test_carrier_contract import (
    ASTERISK,
    CALLER,
    TELNYX,
    TWILIO,
    CarrierEdge,
)
from turncall.orchestrator import pipeline_builder
from turncall.services import mcp_client

pytestmark = pytest.mark.integration

AGENT = {
    **HARNESS_AGENT,
    "first_message": "Hello from the harness.",
    "mcp_servers": [
        {"name": "crm", "transport": "http", "url": "https://mcp.example.com/mcp"}
    ],
}


class FakeMCP:
    """The agent's MCP servers as a call sees them: connected, then closed."""

    made: ClassVar[list[FakeMCP]] = []

    def __init__(self, call_id: UUID, project_id: UUID) -> None:
        self.call_id = call_id
        self.connected = False
        self.closes = 0
        FakeMCP.made.append(self)

    async def connect_servers(self, _servers: Any) -> list:
        self.connected = True
        return []

    def is_mcp_tool(self, _name: str) -> bool:
        return False

    async def close(self) -> None:
        self.closes += 1


@pytest.fixture
def mcp(monkeypatch) -> list[FakeMCP]:
    made: ClassVar[list[FakeMCP]] = []
    monkeypatch.setattr(FakeMCP, "made", made)
    monkeypatch.setattr(mcp_client, "MCPSessionManager", FakeMCP)
    return made


async def _closed(made: list[FakeMCP], *, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not made or any(m.closes == 0 for m in made):
            await asyncio.sleep(0.05)


def _raising_build(monkeypatch) -> None:
    def _broken(**_kwargs: Any) -> Any:
        raise RuntimeError("the build broke")

    monkeypatch.setattr(pipeline_builder, "create_pipeline", _broken)


@pytest.mark.parametrize(
    "edge",
    [pytest.param(e, id=e.name) for e in (TWILIO, TELNYX, ASTERISK)],
)
async def test_a_call_whose_build_raises_closes_its_mcp_sessions(
    edge: CarrierEdge, monkeypatch, redis_env, mcp
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await edge.bind(app, await app.create_agent(AGENT))
        _raising_build(monkeypatch)
        call = await edge.ring(app, CALLER, number)
        ended = await app.sink.wait_for("call.ended")
        await _closed(mcp)
        raised = (
            await app.api.get(
                f"/v1/calls/{call.call_id}/events",
                params={"event_type": "error.raised"},
            )
        ).json()["data"]
        slots = await _live_calls(app.project_id)

    assert ended["call_id"] == call.call_id
    assert ended["payload"]["ended_reason"] == "pipeline_error"
    assert [e["payload"]["stage"] for e in raised] == ["pipeline_build"]
    assert "the build broke" in raised[0]["payload"]["error"]
    assert [(m.connected, m.closes) for m in mcp] == [(True, 1)]
    assert slots == 0


async def test_a_call_whose_build_is_cancelled_closes_its_mcp_sessions(
    monkeypatch, redis_env, mcp
) -> None:
    building: asyncio.Future[asyncio.Task] = asyncio.get_running_loop().create_future()

    async def _stuck(*_args: Any, **_kwargs: Any) -> Any:
        building.set_result(asyncio.current_task())
        await asyncio.Event().wait()

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await TWILIO.bind(app, await app.create_agent(AGENT))
        monkeypatch.setattr(pipeline_builder, "load_call_credentials", _stuck)
        await TWILIO.ring(app, CALLER, number)
        (await asyncio.wait_for(building, 10)).cancel()
        await _closed(mcp)
        slots = await _live_calls(app.project_id)

    assert [(m.connected, m.closes) for m in mcp] == [(True, 1)]
    assert slots == 0


async def test_a_call_that_runs_closes_its_mcp_sessions_once(
    monkeypatch, redis_env, mcp
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await TWILIO.bind(app, await app.create_agent(AGENT))
        call = await TWILIO.ring(app, CALLER, number)
        await call.wait_for_agent_speech(1)
        await call.hang_up()
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        await _closed(mcp)
        await app.post_call_settled()

    assert ended["ended_reason"] == "customer_ended_call"
    assert [(m.connected, m.closes) for m in mcp] == [(True, 1)]
