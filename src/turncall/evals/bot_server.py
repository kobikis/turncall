"""The bot's end of an iteration's loopback socket (ADR-0018, #375).

pipecat 1.13 deprecated the `EvalTransport` that hosted its own server, and its
replacement, `EvalFastAPIWebsocketTransport`, takes a WebSocket somebody else
already accepted, its `eval-connect` message already read. pipecat's only
server for that is its dev runner, so each iteration stands up this one: a
one-route Starlette app under uvicorn, bound to 127.0.0.1 on a port the kernel
picks, serving exactly one connection.

The pipeline is built *after* the socket is accepted, because the transport is
built from it. The harness allows that: it waits `connect_timeout_s` only for
the accept, then up to a minute for `bot-ready`.

Loopback only: the connection is unauthenticated, and it can seed the LLM's
context and raise the function-call report level. Imports pipecat, so it stays
out of the API process (ADR-0004).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from uuid import UUID

import uvicorn
from loguru import logger
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

_HOST = "127.0.0.1"
# How long uvicorn may take to come up, and to let a connection go at shutdown.
_START_TIMEOUT_S = 10.0
_SHUTDOWN_TIMEOUT_S = 5
# The close code for a connection this server will not serve (RFC 6455 1008).
_POLICY_VIOLATION = 1008
_INTERNAL_ERROR = 1011

# Builds the iteration's pipeline on the accepted socket, from the connection's
# `eval-connect` flags. Returns the CallSession.
BuildSession = Callable[[WebSocket, Any], Awaitable[Any]]


class _Server(uvicorn.Server):
    """uvicorn, minus the signal handlers.

    `Server.serve()` takes SIGINT/SIGTERM for itself and re-raises them when it
    exits. Inside the eval worker that would steal the worker's own shutdown,
    once per iteration.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class LoopbackBot:
    """One iteration's bot: a loopback server for the one harness connection.

    `session` is the CallSession once it is built; `pipeline` the task running
    it. A build that raises is kept as `build_error` and the socket closed, so
    the harness stops waiting and the caller can raise the real cause.
    """

    def __init__(self, build: BuildSession, run_id: UUID) -> None:
        self._build = build
        self._run_id = run_id
        self._claimed = False
        self._server: _Server | None = None
        self._serving: asyncio.Task | None = None
        self.session: Any = None
        self.pipeline: asyncio.Task | None = None
        self.build_error: BaseException | None = None

    async def start(self) -> str:
        """Start listening; the harness's URL."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((_HOST, 0))
        port = int(sock.getsockname()[1])
        app = Starlette(routes=[WebSocketRoute("/", self._serve_harness)])
        self._server = _Server(
            uvicorn.Config(
                app,
                lifespan="off",
                # Leave the worker's logging as it is: uvicorn's default config
                # would replace it, once per iteration.
                log_config=None,
                log_level="warning",
                access_log=False,
                ws="websockets-sansio",
                timeout_graceful_shutdown=_SHUTDOWN_TIMEOUT_S,
            )
        )
        self._serving = asyncio.create_task(
            self._server.serve(sockets=[sock]), name=f"eval-server-{self._run_id}"
        )
        await self._until_started()
        return f"ws://{_HOST}:{port}"

    async def _until_started(self) -> None:
        assert self._server is not None and self._serving is not None
        async with asyncio.timeout(_START_TIMEOUT_S):
            while not self._server.started:
                if self._serving.done():
                    # uvicorn swallows a failed startup into SystemExit.
                    self._serving.result()
                    raise RuntimeError("the eval bot server exited at startup")
                await asyncio.sleep(0.01)

    async def _serve_harness(self, websocket: WebSocket) -> None:
        if self._claimed:
            await websocket.close(code=_POLICY_VIOLATION)
            return
        self._claimed = True
        await websocket.accept()
        flags = await _read_eval_connect(websocket)
        if flags is None:
            self.build_error = RuntimeError(
                "the harness did not open with an eval-connect message"
            )
            await _close_quietly(websocket, _POLICY_VIOLATION)
            return
        try:
            self.session = await self._build(websocket, flags)
        except Exception as exc:
            self.build_error = exc
            await _close_quietly(websocket, _INTERNAL_ERROR)
            return
        self.pipeline = asyncio.create_task(
            self.session.start(), name=f"eval-bot-{self._run_id}"
        )
        # The connection lives as long as this handler: returning closes it.
        # Shielded: winding the pipeline down is the caller's (`_stop_bot`).
        await asyncio.shield(self.pipeline)

    def raise_build_error(self) -> None:
        """Raise what stopped the pipeline being built, if anything did."""
        if self.build_error is not None:
            raise self.build_error

    async def close(self) -> None:
        """Stop serving. The pipeline is the caller's to wind down first."""
        if self._server is None or self._serving is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._serving, timeout=_SHUTDOWN_TIMEOUT_S * 2)
        except TimeoutError:
            logger.warning("eval_server_stop_timeout", run_id=str(self._run_id))
            self._serving.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._serving
        except Exception:
            logger.exception("eval_server_error", run_id=str(self._run_id))


async def _read_eval_connect(websocket: WebSocket) -> Any:
    """The connection's flags, from its first message; None if it is not one."""
    from pipecat.evals.serializer import parse_eval_connect

    try:
        first = json.loads(await websocket.receive_text())
    except (WebSocketDisconnect, json.JSONDecodeError, KeyError, RuntimeError):
        return None
    return parse_eval_connect(first)


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=code)
