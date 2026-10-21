"""The bot's end of an iteration's loopback socket (#375).

`EvalFastAPIWebsocketTransport` takes a socket already accepted and its
`eval-connect` read, so the pipeline is built on the connection the harness
opens. What that has to keep from the server pipecat used to host: loopback
only, one harness per iteration, and a pipeline that could not be built
reported as itself rather than as a harness that could not connect.
"""

import asyncio
import signal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pipecat.evals.serializer import EvalConnectionFlags, eval_connect_message
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from turncall.domain.models import AgentConfig
from turncall.evals.bot_server import LoopbackBot

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Session:
    """A CallSession stand-in whose pipeline runs until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.failure = None
        self.pipeline_errors: list[str] = []

    async def start(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


async def _stop(bot: LoopbackBot) -> None:
    if bot.pipeline is not None:
        bot.pipeline.cancel()
        await asyncio.gather(bot.pipeline, return_exceptions=True)
    await bot.close()


async def test_the_pipeline_is_built_on_the_harness_connection() -> None:
    session = _Session()
    seen: dict[str, Any] = {}

    async def build(websocket, flags):
        seen["websocket"], seen["flags"] = websocket, flags
        return session

    bot = LoopbackBot(build, uuid4())
    url = await bot.start()
    try:
        assert url.startswith("ws://127.0.0.1:")
        assert not url.endswith(":0")
        assert seen == {}, "nothing is built before the harness connects"

        async with connect(url) as ws:
            await ws.send(eval_connect_message(EvalConnectionFlags(skip_tts=True)))
            await asyncio.wait_for(session.started.wait(), 5)

        assert seen["flags"] == EvalConnectionFlags(skip_tts=True)
        assert bot.session is session
        bot.raise_build_error()
    finally:
        await _stop(bot)


async def test_a_build_that_raises_closes_the_socket_and_is_kept() -> None:
    boom = RuntimeError("no key for deepgram")

    async def build(_websocket, _flags):
        raise boom

    bot = LoopbackBot(build, uuid4())
    url = await bot.start()
    try:
        async with connect(url) as ws:
            await ws.send(eval_connect_message(EvalConnectionFlags()))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        with pytest.raises(RuntimeError) as caught:
            bot.raise_build_error()
        assert caught.value is boom
    finally:
        await _stop(bot)


async def test_a_connection_that_does_not_open_with_eval_connect_is_refused() -> None:
    built = MagicMock()

    async def build(websocket, flags):
        built(websocket, flags)

    bot = LoopbackBot(build, uuid4())
    url = await bot.start()
    try:
        async with connect(url) as ws:
            await ws.send('{"label": "rtvi-ai", "type": "client-ready"}')
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
            assert ws.close_code == 1008
        built.assert_not_called()
        with pytest.raises(RuntimeError, match="eval-connect"):
            bot.raise_build_error()
    finally:
        await _stop(bot)


async def test_one_harness_per_iteration() -> None:
    session = _Session()

    async def build(_websocket, _flags):
        return session

    bot = LoopbackBot(build, uuid4())
    url = await bot.start()
    try:
        async with connect(url) as first:
            await first.send(eval_connect_message(EvalConnectionFlags()))
            await asyncio.wait_for(session.started.wait(), 5)
            # Closed before the handshake completes: refused at the upgrade.
            with pytest.raises(InvalidStatus):
                async with connect(url):
                    pass
            assert bot.session is session
    finally:
        await _stop(bot)


async def test_the_harness_readiness_probe_logs_nothing(caplog) -> None:
    """pipecat's client checks the bot is up with a bare TCP connect it closes
    at once. The `websockets` server pipecat's old transport ran logged that
    as a failed handshake with a traceback, once per iteration, which the
    worker had to filter; this server says nothing."""
    import logging

    async def build(_websocket, _flags):
        return _Session()

    bot = LoopbackBot(build, uuid4())
    url = await bot.start()
    port = int(url.rsplit(":", 1)[1])
    try:
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(0.2)
    finally:
        await _stop(bot)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert bot.session is None


async def test_the_server_leaves_the_workers_signal_handlers_alone() -> None:
    """uvicorn takes SIGTERM while serving and re-raises it on exit; inside the
    eval worker that would steal the worker's own shutdown."""

    async def build(_websocket, _flags):
        return _Session()

    before = signal.getsignal(signal.SIGTERM)
    bot = LoopbackBot(build, uuid4())
    await bot.start()
    try:
        assert signal.getsignal(signal.SIGTERM) is before
    finally:
        await _stop(bot)
    assert signal.getsignal(signal.SIGTERM) is before


async def test_run_iteration_reports_the_build_failure_not_the_closed_socket() -> None:
    """Before #375 the pipeline was built before the harness started, so its
    exception left `run_iteration` as itself. It still has to: the harness only
    sees a socket that closed."""
    from turncall.evals import harness as harness_mod

    boom = RuntimeError("the agent config cannot be built")

    def harness_that_connects(_parsed, _kind, url, **_kw):
        async def run():
            async with connect(url) as ws:
                await ws.send(eval_connect_message(EvalConnectionFlags()))
                await asyncio.wait_for(ws.recv(), 5)

        return SimpleNamespace(run=run)

    with (
        patch(
            "turncall.orchestrator.pipeline_builder.build_call_pipeline",
            MagicMock(side_effect=boom),
        ),
        patch.object(harness_mod, "_build_session", harness_that_connects),
    ):
        with pytest.raises(RuntimeError) as caught:
            await harness_mod.run_iteration(
                parsed=MagicMock(),
                kind=MagicMock(),
                target=SimpleNamespace(
                    project_id=uuid4(), config=AgentConfig(), agent_id=uuid4()
                ),
                modality=MagicMock(),
                settings=SimpleNamespace(),
                session_factory=MagicMock(),
                run_id=uuid4(),
                tool_mocks=MagicMock(),
                timeout_s=10,
            )
    assert caught.value is boom
