"""Running a call (#491): one module under every voice edge.

An edge hands `run_call` the call it opened and a factory for its transport;
running does the rest — the drain count, the live-call slot, MCP, the
ambience mixer, the call context, the build and the start. These tests drive
it from outside, the way an edge does, on the harness's real Postgres and
Redis with stub providers, and check what the world sees afterwards: whether
the slot is held, whether the drain counts the call, whether the MCP fake was
closed, what reached the transport, and what the call row says.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from pipecat.frames.frames import StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport

from tests.accepted import accepted
from tests.harness import HARNESS_AGENT, Script, run_app
from turncall.domain.enums import CallDirection
from turncall.orchestrator.call_runner import CallToRun, TransportFactory, run_call
from turncall.services import drain, limits

pytestmark = pytest.mark.integration

SCRIPT = Script(caller=(), agent=())
# The shortest call a config may ask for: one a test fails to end is ended by
# its own watchdog, rather than holding the app's shutdown for an hour.
AGENT = {**HARNESS_AGENT, "max_call_duration_seconds": 60}
MCP_AGENT = {
    **AGENT,
    "mcp_servers": [{"name": "crm", "transport": "http", "url": "https://x/mcp"}],
}


class _Passthrough(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        await self.push_frame(frame, direction)


class FakeTransport(BaseTransport):
    """A transport with no wire behind it: frames pass through, and the
    caller hangs up when the test says so."""

    def __init__(self) -> None:
        super().__init__()
        self._input, self._output = _Passthrough(), _Passthrough()
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def input(self) -> FrameProcessor:
        return self._input

    def output(self) -> FrameProcessor:
        return self._output

    async def started(self) -> None:
        """The call is up: its pipeline started."""
        await self._output.started.wait()

    async def hang_up(self) -> None:
        """The caller leaving a call that is up: its pipeline is running."""
        await asyncio.wait_for(self.started(), 10)
        await self._call_event_handler("on_client_disconnected", self)


class Edge:
    """An edge's transport factory: records the mixer it was handed."""

    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.mixers: list[Any] = []

    def factory(self) -> TransportFactory:
        return TransportFactory(make=self._make, sample_rate=16000)

    def _make(self, mixer: Any) -> FakeTransport:
        self.mixers.append(mixer)
        return self.transport


class FakeMCP:
    """An agent's MCP servers, from the call's side: connected, then closed —
    and in which task each happened, since an MCP transport's cancel scopes
    must be exited in the task that entered them."""

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.connecting = asyncio.Event()
        self.closed = False
        self.tasks: dict[str, asyncio.Task[Any] | None] = {}

    def __call__(self, **_kwargs: Any) -> FakeMCP:
        return self

    async def connect_servers(self, _servers: Any) -> list[Any]:
        self.tasks["connect"] = asyncio.current_task()
        self.connecting.set()
        if self.hang:
            await asyncio.sleep(30)
        return []

    async def close(self) -> None:
        self.tasks["close"] = asyncio.current_task()
        self.closed = True


async def _opened(app: Any, config: dict[str, Any]) -> CallToRun:
    """What an edge holds once it opened a call: the row with its inline
    agent, and the call's live-call slot taken."""
    from turncall.storage.database import create_session_factory, get_engine
    from turncall.storage.repositories import call_repo

    project_id, call_id = uuid.UUID(app.project_id), uuid.uuid4()
    session_factory = create_session_factory(get_engine())
    async with session_factory() as session:
        await call_repo.create_call(
            session,
            project_id=project_id,
            call_id=call_id,
            direction=CallDirection.INBOUND,
            active_agent_id=None,
            metadata_json={"transport": "webrtc", "dynamic_config": config},
        )
        await session.commit()
    assert await limits.claim_live_call(project_id, call_id, 1)
    return CallToRun(
        call_id=call_id,
        project_id=project_id,
        agent=accepted(config),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=session_factory,
    )


async def _slot_held(call: CallToRun) -> bool:
    """Whether the project's only slot is taken (by the call under test)."""
    other = uuid.uuid4()
    if await limits.claim_live_call(call.project_id, other, 1):
        await limits.release_live_call(call.project_id, other)
        return False
    return True


async def _call_status(call: CallToRun) -> str:
    from turncall.storage.repositories import call_repo

    assert call.session_factory is not None
    async with call.session_factory() as session:
        row = await call_repo.get_call_by_id(session, call.call_id)
    return str(row.status)


async def _until(check: Any, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


async def _redis_or_skip() -> None:
    from turncall.storage.redis import get_redis

    try:
        await get_redis().ping()
    except Exception:
        pytest.skip("Redis not reachable")


async def test_a_running_call_holds_its_slot_and_is_counted(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        call, edge = await _opened(app, AGENT), Edge()

        await run_call(call, edge.factory(), own_task=True)
        counted, held = drain.active_calls(), await _slot_held(call)

        await edge.transport.hang_up()
        await _until(_drained)
        freed = not await _slot_held(call)

    assert (counted, held) == (1, True)
    assert freed


async def test_a_running_call_answers_its_control_channel(monkeypatch):
    """`POST /v1/calls/{id}/messages` reaches a call that runs here, whatever
    edge opened it (#215): live while it runs, `409 call_not_live` after."""
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        call, edge = await _opened(app, AGENT), Edge()
        url = f"/v1/calls/{call.call_id}/messages"

        await run_call(call, edge.factory(), own_task=True)
        await asyncio.wait_for(edge.transport.started(), 10)
        live = await app.api.post(url, json={"message": "VIP", "role": "system"})

        await edge.transport.hang_up()
        await _until(_drained)
        after = await app.api.post(url, json={"message": "VIP"})

    assert live.status_code == 200, live.text
    assert live.json()["data"]["details"] == {"role": "system"}
    assert after.status_code == 409
    assert after.json()["code"] == "call_not_live"


async def test_a_call_in_its_own_task_opens_and_closes_mcp_in_that_task(
    monkeypatch,
):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        mcp = FakeMCP()
        monkeypatch.setattr("turncall.services.mcp_client.MCPSessionManager", mcp)
        call, edge = await _opened(app, MCP_AGENT), Edge()

        await run_call(call, edge.factory(), own_task=True)
        await edge.transport.hang_up()
        await _until(_drained)

    assert mcp.closed
    assert mcp.tasks["connect"] is mcp.tasks["close"]
    assert mcp.tasks["connect"] is not asyncio.current_task()


async def _drained() -> bool:
    return drain.active_calls() == 0


async def test_the_transport_is_built_around_the_calls_mixer(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        sound = await app.api.post(
            "/v1/ambience-sounds", files={"file": ("room.wav", _wav(), "audio/wav")}
        )
        assert sound.status_code == 201, sound.text
        config = {**AGENT, "ambience": {"sound": sound.json()["data"]["id"]}}
        call, edge = await _opened(app, config), Edge()

        await run_call(call, edge.factory(), own_task=True)
        await edge.transport.hang_up()
        await _until(_drained)

    assert len(edge.mixers) == 1
    assert type(edge.mixers[0]).__name__ == "SoundfileMixer"


@pytest.mark.parametrize("own_task", [True, False])
async def test_a_build_that_raises_reaches_the_edge_closes_mcp_and_frees_the_slot(
    monkeypatch, own_task
):
    from turncall.orchestrator import pipeline_factory

    def _refusing_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the provider refused the key")

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        mcp = FakeMCP()
        monkeypatch.setattr("turncall.services.mcp_client.MCPSessionManager", mcp)
        monkeypatch.setattr(pipeline_factory, "_create_llm_service", _refusing_llm)
        call = await _opened(app, MCP_AGENT)

        with pytest.raises(RuntimeError, match="refused the key"):
            await run_call(call, Edge().factory(), own_task=own_task)
        await _until(_drained)
        held = await _slot_held(call)
        status = await _call_status(call)

    assert mcp.closed
    assert not held
    assert status == "failed"


async def test_a_failure_before_the_build_reaches_the_edge_as_itself(monkeypatch):
    """Nothing outside TurnCall fails before the build, so the fault is put
    in the drain count: whatever raises there, an edge waiting on the call's
    own task hears that error — not a cancellation that never happened."""
    import contextlib

    @contextlib.contextmanager
    def _broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the drain count broke")
        yield

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        call = await _opened(app, AGENT)
        monkeypatch.setattr(drain, "live_call", _broken)

        with pytest.raises(RuntimeError, match="the drain count broke"):
            await run_call(call, Edge().factory(), own_task=True)


async def test_a_call_cancelled_before_it_starts_closes_mcp(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        mcp = FakeMCP(hang=True)
        monkeypatch.setattr("turncall.services.mcp_client.MCPSessionManager", mcp)
        call = await _opened(app, MCP_AGENT)

        running = asyncio.create_task(run_call(call, Edge().factory(), own_task=False))
        await asyncio.wait_for(mcp.connecting.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        held = await _slot_held(call)

    assert mcp.closed
    assert not held
    assert drain.active_calls() == 0


def _wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    rng = np.random.default_rng(0)
    sf.write(buf, rng.uniform(-0.3, 0.3, int(16000 * seconds)), 16000, format="WAV")
    return buf.getvalue()


async def test_a_call_with_no_database_runs_and_records_nothing(monkeypatch):
    """The website demo's call (#493): no row, no slot, no event — but
    counted, so a SIGTERM still waits for it."""
    from loguru import logger
    from sqlalchemy import select

    from turncall.storage.database import create_session_factory, get_engine
    from turncall.storage.models import CallEventRow
    from turncall.storage.repositories import call_repo

    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _redis_or_skip()
        errors: list[str] = []
        sink = logger.add(
            lambda m: errors.append(str(m)),
            level="ERROR",
            filter=lambda record: record["name"].startswith("turncall"),
        )
        project_id, edge = uuid.UUID(app.project_id), Edge()
        call = CallToRun(
            call_id=uuid.uuid4(),
            project_id=project_id,
            agent=accepted(AGENT),
            call_sid="webrtc",
            stream_sid="webrtc",
            session_factory=None,
        )
        try:
            task = await run_call(call, edge.factory(), own_task=True)
            counted, slot_free = drain.active_calls(), not await _slot_held(call)

            await edge.transport.hang_up()
            assert task is not None
            await asyncio.wait_for(task, 10)
        finally:
            logger.remove(sink)
        async with create_session_factory(get_engine())() as session:
            row = await call_repo.get_call_by_id(session, call.call_id)
            events = (
                await session.execute(
                    select(CallEventRow).where(CallEventRow.call_id == call.call_id)
                )
            ).all()

    assert (counted, slot_free) == (1, True)
    assert drain.active_calls() == 0
    assert row is None
    assert events == []
    assert errors == []
