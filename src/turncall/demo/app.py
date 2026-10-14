"""The demo server: WebRTC signaling for the website's talk-to-an-agent button.

Speaks what pipecat's browser `SmallWebRTCTransport` sends — the same
`POST`/`PATCH /connect` pair as `/v1/webrtc/connect` — but authenticates
nothing, stores nothing and reaches no project: whatever fronts it (the
website's Worker) decides who may call. Each call runs the real pipeline
(`build_call_pipeline`) with no database behind it, so `CallContext.records`
is false and nothing is written anywhere.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import fields
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from turncall.config.settings import Settings
from turncall.domain.models import AgentConfig
from turncall.services import drain

# A demo call belongs to no project; the zero UUID says so in its logs.
DEMO_PROJECT_ID = UUID(int=0)


def create_demo_app(agents: dict[str, AgentConfig], settings: Settings) -> FastAPI:
    """The demo server for `agents`. The first is the default."""
    from pipecat.transports.smallwebrtc.request_handler import (
        ConnectionMode,
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )

    from turncall.orchestrator.transport_factory import ice_servers

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # A container is stopped with SIGTERM (a rollout, a host going away)
        # and given time to exit: the call on the line finishes first (#199).
        drain.install_sigterm_drain()
        yield
        drain.uninstall_sigterm_drain()

    app = FastAPI(
        title="TurnCall demo", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    handler = SmallWebRTCRequestHandler(
        ice_servers=ice_servers(), connection_mode=ConnectionMode.MULTIPLE
    )
    signaling_keys = {f.name for f in fields(SmallWebRTCRequest)} | {"requestData"}
    max_calls = settings.demo.max_calls
    # Calls held, counted from the offer — before anything awaits — so two
    # offers at once cannot both take the last place.
    held = {"calls": 0}
    running: set[asyncio.Task[None]] = set()
    default = next(iter(agents))

    def release() -> None:
        held["calls"] -= 1
        if settings.demo.exit_after_call and held["calls"] == 0:
            # A container per visitor: its one call is over, so the process
            # ends and the container with it, instead of idling on the bill.
            logger.info("demo_exit_after_call")
            os.kill(os.getpid(), signal.SIGTERM)

    @app.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Whether a call can start here now: the Worker asks before it hands
        over a visitor, so a busy process is a different container."""
        status = (
            "draining"
            if drain.is_draining()
            else "busy"
            if held["calls"] >= max_calls
            else "ready"
        )
        return JSONResponse(
            status_code=200 if status == "ready" else 503,
            content={"status": status, "calls": held["calls"]},
        )

    @app.get("/agents")
    async def agent_names() -> dict[str, Any]:
        return {"agents": list(agents), "default": default}

    @app.post("/connect")
    async def connect(request: Request) -> JSONResponse:
        body = await _json_object(request)
        if body is None:
            return _error(400, "Expected a JSON object.")
        try:
            offer = SmallWebRTCRequest.from_dict(
                {k: v for k, v in body.items() if k in signaling_keys}
            )
        except Exception as exc:
            return _error(400, f"Invalid WebRTC signaling payload: {exc}")

        if offer.pc_id and offer.pc_id in handler._pcs_map:
            # A renegotiation of a call already running: pipecat reuses the
            # connection and never calls back, so nothing new starts.
            return await _answer(handler, offer, _no_new_call)

        name = _requested_agent(offer.request_data, body) or default
        config = agents.get(name)
        if config is None:
            return _error(404, f"No demo agent named {name!r}.")
        if drain.is_draining():
            return _error(503, "This demo server is shutting down.")
        if held["calls"] >= max_calls:
            return _error(429, "This demo line is busy.")
        held["calls"] += 1

        started: list[asyncio.Task[None]] = []
        failure: list[tuple[Any, Exception]] = []

        async def on_connection(connection: Any) -> None:
            try:
                session = await _build_call(config, connection, settings)
            except Exception as exc:
                logger.exception("demo_call_build_failed", agent=name)
                failure.append((connection, exc))
                raise
            task = asyncio.create_task(_run(session, config))
            running.add(task)
            task.add_done_callback(running.discard)
            task.add_done_callback(lambda _task: release())
            started.append(task)
            logger.info("demo_call_started", agent=name, pc_id=connection.pc_id)

        try:
            response = await _answer(handler, offer, on_connection)
        finally:
            if not started:
                release()
        if failure:
            connection, exc = failure[0]
            await connection.disconnect()
            return _build_error(exc)
        return response

    @app.patch("/connect")
    async def ice_candidates(request: Request) -> JSONResponse:
        from pipecat.transports.smallwebrtc.request_handler import (
            IceCandidate,
            SmallWebRTCPatchRequest,
        )

        body = await _json_object(request)
        if body is None:
            return _error(400, "Expected a JSON object.")
        candidates = [
            IceCandidate(
                candidate=c.get("candidate", ""),
                sdp_mid=c.get("sdpMid", c.get("sdp_mid", "")),
                sdp_mline_index=c.get("sdpMLineIndex", c.get("sdp_mline_index", 0)),
            )
            for c in body.get("candidates") or []
            if isinstance(c, dict)
        ]
        await handler.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=str(body.get("pc_id", "")), candidates=candidates
            )
        )
        return JSONResponse(content={"status": "ok"})

    return app


async def _run(session: Any, config: AgentConfig) -> None:
    # Counted, so a SIGTERM waits for it.
    with drain.live_call(session.call_id, config.max_call_duration_seconds):
        await session.start()


async def _build_call(config: AgentConfig, connection: Any, settings: Settings) -> Any:
    """The call's CallSession, built and not yet started."""
    from turncall.orchestrator.pipeline_builder import build_call_pipeline
    from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID, CallContext
    from turncall.orchestrator.transport_factory import (
        WEBRTC_SAMPLE_RATE,
        create_whatsapp_transport,
    )

    call_context = CallContext(
        call_id=uuid4(),
        project_id=DEMO_PROJECT_ID,
        agent_id=DYNAMIC_AGENT_ID,
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=None,
    )
    return await build_call_pipeline(
        config=config,
        # No room to mix in: the demo refuses an agent with ambience, whose
        # sound lives in a project's storage.
        transport=create_whatsapp_transport(connection, mixer=None),
        call_context=call_context,
        settings=settings,
        session_factory=None,
        audio_sample_rate=WEBRTC_SAMPLE_RATE,
    )


async def _answer(handler: Any, offer: Any, on_connection: Any) -> JSONResponse:
    answer = await handler.handle_web_request(offer, on_connection)
    if answer is None:
        return _error(500, "Failed to create a WebRTC answer.")
    return JSONResponse(content=answer)


async def _no_new_call(_connection: Any) -> None:
    raise RuntimeError("a renegotiation opened a new connection")


def _requested_agent(request_data: Any, body: dict[str, Any]) -> str | None:
    data = request_data if isinstance(request_data, dict) else {}
    name = data.get("demo") or body.get("demo")
    return str(name) if name else None


def _build_error(exc: Exception) -> JSONResponse:
    """A call that could not start. pipecat answers the offer anyway, which
    would connect the browser to a peer with nothing behind it."""
    from turncall.services.credentials import MissingProviderKeyError

    if isinstance(exc, MissingProviderKeyError):
        return _error(
            503,
            "The demo server has no key for " + ", ".join(exc.accounts) + ".",
            missing_keys=exc.accounts,
        )
    return _error(500, "The agent could not start.")


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})
