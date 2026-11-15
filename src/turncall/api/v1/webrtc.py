"""WebRTC signaling via Pipecat SmallWebRTCTransport.

Pipecat 1.0 uses SmallWebRTCRequestHandler for signaling:
  - POST /connect  — SDP offer/answer exchange
  - PATCH /connect — ICE candidate trickle
"""

from dataclasses import dataclass, fields, replace
from functools import partial
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ErrorCode, NotFoundError, RateLimitedError
from turncall.auth import WriteAuth
from turncall.config import get_settings
from turncall.domain.carriers import DEFAULT_CARRIER
from turncall.orchestrator.avatar import skip_reason
from turncall.orchestrator.call_runner import CallToRun, TransportFactory, run_call
from turncall.orchestrator.transport_factory import (
    WEBRTC_SAMPLE_RATE,
    create_whatsapp_transport,
    ice_servers,
)
from turncall.orchestrator.webrtc_offer import bundled
from turncall.services.call_agents import call_init_agent, stored_agent
from turncall.services.call_opening import (
    CallParties,
    CallRefusal,
    ChosenAgent,
    EdgeHandles,
    open_call,
)
from turncall.services.credentials import MissingProviderKeyError
from turncall.services.limits import release_live_call
from turncall.services.outbound_url import check_outbound_url
from turncall.services.template_renderer import check_variables
from turncall.storage.database import create_session_factory, get_engine

router = APIRouter(prefix="/webrtc", tags=["webrtc"])


# SmallWebRTCRequest.from_dict is a bare cls(**data): extra top-level keys like
# agent_id (honored via body.get() in the handler) must be stripped before it.
# Derived from the dataclass so a Pipecat upgrade can't silently drop a field;
# requestData is the camelCase alias from_dict accepts.
_SIGNALING_KEYS = {f.name for f in fields(SmallWebRTCRequest)} | {"requestData"}

# Module-level request handler — manages peer connections across requests
_request_handler = SmallWebRTCRequestHandler(
    ice_servers=ice_servers(),
    connection_mode=ConnectionMode.MULTIPLE,
)

# The call each held peer connection carries, and its project: pc_id ->
# (call_id, project_id), from the connection's start until it closes. A
# renegotiation names one of these, and is that call — never a new one.
_calls_by_pc: dict[str, tuple[UUID, UUID]] = {}


def _is_uuid(value: object) -> bool:
    """Whether a request's agent_id is a UUID string. Not a string at all
    (`123`) is no: UUID() raises AttributeError on one, not ValueError."""
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


async def _json_object(request: Request) -> dict[str, Any]:
    """The request's body as a JSON object; 400 otherwise."""
    try:
        body = await request.json()
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request body must be valid JSON",
        ) from exc
    if not isinstance(body, dict):
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request body must be a JSON object",
        )
    return body


@router.post("/connect", status_code=200)
async def webrtc_connect(
    request: Request,
    auth: WriteAuth,
    session: DbSession,
) -> JSONResponse:
    """WebRTC signaling endpoint — SDP offer/answer exchange.

    Accepts SDP offer, creates pipeline, returns SDP answer.
    """
    body = await _json_object(request)
    logger.info("WebRTC connect request: {keys}", keys=list(body.keys()))

    try:
        webrtc_request = SmallWebRTCRequest.from_dict(
            bundled({k: v for k, v in body.items() if k in _SIGNALING_KEYS})
        )
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message=f"Invalid WebRTC signaling payload: {exc}",
        ) from exc

    # pipecat reuses a peer connection whose pc_id it holds, and never calls
    # back for it: the offer renegotiates a call already running. Asked before
    # anything a new call does — call-init, a live-call slot, a call row —
    # each of which a renegotiation used to repeat, leaving a call stuck at
    # `initiated` and its id in the answer. Its own map, read where it reads it.
    pc_id = webrtc_request.pc_id
    if pc_id and pc_id in _request_handler._pcs_map:
        return await _renegotiate(webrtc_request, auth.project_id)

    # Extract agent_id / server_url from request_data
    request_data = webrtc_request.request_data or {}
    if isinstance(request_data, dict):
        agent_id_str = request_data.get("agent_id") or body.get("agent_id")
        server_url = request_data.get("server_url") or body.get("server_url")
        server_url_secret = request_data.get("server_url_secret") or body.get(
            "server_url_secret"
        )
        raw_variables = request_data.get("variables") or body.get("variables")
    else:
        agent_id_str = body.get("agent_id")
        server_url = body.get("server_url")
        server_url_secret = body.get("server_url_secret")
        raw_variables = body.get("variables")
    variables = _request_variables(raw_variables)

    if not agent_id_str and not server_url:
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="agent_id or server_url required in requestData",
        )

    # Validate agent_id shape here so a malformed value is a clean 400, not a
    # 500 when the source does UUID(agent_id_str).
    if agent_id_str and not _is_uuid(agent_id_str):
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message=f"agent_id must be a UUID, got {agent_id_str!r}",
        )

    # server_url must be https:// (or an in-cluster Service) wherever plain
    # http is not allowed (#340). Not a pydantic body, so a 400, not a 422.
    # Checked only when it is used: an agent_id wins over it.
    settings = get_settings()
    if server_url and not agent_id_str:
        if not isinstance(server_url, str):
            raise ApiError(
                status_code=400,
                code=ErrorCode.VALIDATION_ERROR,
                message="server_url must be a string",
            )
        try:
            server_url = check_outbound_url(
                server_url, label="server_url", settings=settings
            )
        except ValueError as exc:
            raise ApiError(
                status_code=400,
                code=ErrorCode.VALIDATION_ERROR,
                message=str(exc),
            ) from exc

    # Opened like every voice call (#494): the slot, the agent the request
    # names or its call-init answers, the row and its start events. A refusal
    # is answered as this request's error, and nothing of the call is left,
    # its slot included.
    source = _BrowserSource(
        project_id=auth.project_id,
        agent_id=agent_id_str,
        server_url=server_url,
        server_url_secret=server_url_secret,
        variables=variables,
    )
    try:
        opened = await open_call(source, project_id=auth.project_id, session=session)
    except Exception as exc:
        # A row that cannot be written gave its slot back at once (#489).
        logger.exception("webrtc call not opened")
        raise ApiError(
            status_code=500,
            code=ErrorCode.PIPELINE_START_FAILED,
            message="The call could not be opened",
        ) from exc
    if isinstance(opened, CallRefusal):
        raise _refused(opened, agent_id=agent_id_str)
    call_id = opened.call_id

    call = CallToRun.of(opened, session_factory=create_session_factory(get_engine()))
    config = call.agent.config
    # A browser call is the one that can show an Avatar. Whether this one will
    # is the build's decision (`avatar_gate`, which records why not); the
    # transport only needs to know whether to send video.
    avatar_on = (
        config.avatar.enabled and skip_reason(config, carries_video=True) is None
    )

    # pipecat's handler logs an exception from this callback and answers the
    # offer anyway, so a call that could not start used to connect the browser
    # to a peer with no pipeline behind it: silence, then nothing.
    build_failure: list[tuple[SmallWebRTCConnection, Exception]] = []

    # Use SmallWebRTCRequestHandler — it creates/reuses the connection
    async def on_connection(connection: SmallWebRTCConnection) -> None:
        """Callback invoked when a new WebRTC connection is established."""
        transport = TransportFactory(
            make=partial(create_whatsapp_transport, connection, video_out=avatar_on),
            sample_rate=WEBRTC_SAMPLE_RATE,
            carries_video=True,
        )
        try:
            # Returns once the pipeline is built; the call runs in a task of its
            # own so any MCP servers are connected and closed inside it.
            await run_call(call, transport, own_task=True, settings=settings)
        except Exception as exc:
            # The call is already failed with its reason (build_call_pipeline).
            logger.exception("webrtc pipeline build failed", call_id=str(call_id))
            build_failure.append((connection, exc))
            raise

        _calls_by_pc[connection.pc_id] = (call_id, auth.project_id)

        @connection.event_handler("closed")
        async def _forget(closed: SmallWebRTCConnection) -> None:
            _calls_by_pc.pop(closed.pc_id, None)

    try:
        answer = await _request_handler.handle_web_request(
            webrtc_request, on_connection
        )
    except BaseException:
        await release_live_call(auth.project_id, call_id)
        raise

    if build_failure:
        connection, exc = build_failure[0]
        await connection.disconnect()
        raise ApiError(
            status_code=500,
            code=ErrorCode.PIPELINE_START_FAILED,
            message=f"The agent could not start (call {call_id}): {exc}",
            details=(
                {"missing_keys": exc.accounts}
                if isinstance(exc, MissingProviderKeyError)
                else None
            ),
        )

    if answer is None:
        await release_live_call(auth.project_id, call_id)
        raise ApiError(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Failed to create WebRTC answer",
        )

    logger.info("WebRTC call started: call={call_id}", call_id=str(call_id))
    # The call id rides back with the answer (#87). Additive: a client reading
    # `sdp`/`type` is unaffected, and without it a browser that just held a
    # conversation cannot say which call that was — so it cannot fetch the
    # transcript, link to the recording, or derive a scenario from it. The id
    # was only ever logged.
    if isinstance(answer, dict):
        answer = {**answer, "call_id": str(call_id)}
    return JSONResponse(content=answer)


def _held_call_id(pc_id: str | None, project_id: UUID) -> UUID:
    """The call a held connection carries, if it is the project's.

    A pc_id is the client's to repeat, not a credential: one whose call is
    another project's, or that no call of ours claims, is a 404 either way, so
    the answer never says whether the pc_id exists.
    """
    held = _calls_by_pc.get(pc_id or "")
    if held is None or held[1] != project_id:
        raise NotFoundError("WebRTC connection", str(pc_id))
    return held[0]


async def _renegotiate(
    webrtc_request: SmallWebRTCRequest, project_id: UUID
) -> JSONResponse:
    """Answer a renegotiation of a held connection, under its call's id."""
    call_id = _held_call_id(webrtc_request.pc_id, project_id)

    async def _no_new_call(_connection: SmallWebRTCConnection) -> None:
        raise RuntimeError("a renegotiation opened a new connection")

    answer = await _request_handler.handle_web_request(webrtc_request, _no_new_call)
    if answer is None:
        raise ApiError(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Failed to create WebRTC answer",
        )
    # A restart (`restart_pc`) replaces the peer connection, and the new one has
    # a pc_id of its own: the client trickles and renegotiates under it from
    # here, and its `closed` is what forgets it.
    old_pc_id, new_pc_id = webrtc_request.pc_id or "", answer.get("pc_id")
    if new_pc_id and new_pc_id != old_pc_id:
        _calls_by_pc.pop(old_pc_id, None)
        _calls_by_pc[new_pc_id] = (call_id, project_id)
    logger.info("WebRTC renegotiated: call={call_id}", call_id=str(call_id))
    return JSONResponse(content={**answer, "call_id": str(call_id)})


@router.patch("/connect", status_code=200)
async def webrtc_ice_candidate(request: Request, auth: WriteAuth) -> JSONResponse:
    """WebRTC signaling endpoint — ICE candidate trickle.

    Authorised as its connection was (#342): a key that could have opened it,
    for the project whose call holds the `pc_id`. Anything else is answered
    before pipecat sees a candidate.
    """
    body = await _json_object(request)
    pc_id = str(body.get("pc_id") or "")
    _held_call_id(pc_id, auth.project_id)

    raw = body.get("candidates")
    candidates = [
        IceCandidate(
            candidate=c.get("candidate", ""),
            sdp_mid=c.get("sdpMid", c.get("sdp_mid", "")),
            sdp_mline_index=c.get("sdpMLineIndex", c.get("sdp_mline_index", 0)),
        )
        for c in (raw if isinstance(raw, list) else [])
        if isinstance(c, dict)
    ]
    patch_request = SmallWebRTCPatchRequest(pc_id=pc_id, candidates=candidates)

    await _request_handler.handle_patch_request(patch_request)
    return JSONResponse(content={"status": "ok"})


def _request_variables(raw: Any) -> dict[str, str]:
    """The `{{variables}}` a connect request carries, as strings: a JSON
    object of scalars, bounded like a chat session's. 400 otherwise."""
    if raw is None:
        return {}
    scalar = (str, int, float, bool)
    if not isinstance(raw, dict) or not all(
        isinstance(v, scalar) for v in raw.values()
    ):
        raise ApiError(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="variables must be an object of strings, numbers or booleans",
        )
    try:
        return check_variables({str(k): str(v) for k, v in raw.items()})
    except ValueError as exc:
        raise ApiError(
            status_code=400, code=ErrorCode.VALIDATION_ERROR, message=str(exc)
        ) from exc


# A browser call has no numbers and no carrier: its row keeps the default
# carrier (`calls.provider`) as it always has, and its events say `webrtc`.
_NO_NUMBERS = CallParties(from_number="", to_number="")
_BROWSER = EdgeHandles(carrier=DEFAULT_CARRIER, transport="webrtc")


@dataclass(frozen=True)
class _BrowserSource:
    """A connect request, as `open_call` takes it: the agent it names, or
    the one its call-init answers — chosen as a number's call chooses
    either. A refusal is the request's to answer (`_refused`), and the
    browser hears it: none is announced as `call.rejected`."""

    project_id: UUID
    agent_id: str | None
    server_url: str | None
    server_url_secret: str | None
    variables: dict[str, str]
    parties: CallParties = _NO_NUMBERS
    handles: EdgeHandles = _BROWSER
    refused_unanswered: bool = False

    async def choose_agent(
        self, session: AsyncSession, *, call_id: UUID
    ) -> ChosenAgent | CallRefusal:
        chosen = await self._chosen(session, call_id=call_id)
        if isinstance(chosen, CallRefusal):
            return chosen
        # The request's own win a clash with call-init's, as an outbound
        # dial's do.
        metadata = chosen.metadata
        variables = {**metadata.template_variables, **self.variables}
        return replace(chosen, metadata=replace(metadata, template_variables=variables))

    async def _chosen(
        self, session: AsyncSession, *, call_id: UUID
    ) -> ChosenAgent | CallRefusal:
        if self.agent_id:
            return await stored_agent(
                session,
                UUID(self.agent_id),
                project_id=self.project_id,
                call_id=call_id,
            )
        return await call_init_agent(
            session,
            server_url=str(self.server_url),
            secret=self.server_url_secret,
            call_type="webrtc",
            parties=self.parties,
            handles=self.handles,
            variables=self.variables,
            project_id=self.project_id,
            call_id=call_id,
        )


def _refused(refusal: CallRefusal, *, agent_id: str | None) -> ApiError:
    """The answer to a connect `open_call` refused. Over the live-call limit
    (#224) is a 429 naming it. A named agent that is not the project's is a
    404, one acceptance refuses a 422 naming the field; a `server_url` that
    names no agent, or one that cannot run, a 502."""
    if refusal.busy:
        limit = refusal.live_call_limit
        return RateLimitedError(
            f"This project is at its limit of {limit} live calls",
            {"limit": "live_calls", "max": limit},
        )
    refused = refusal.refusal
    if agent_id and refused is None:
        return NotFoundError("Agent", agent_id)
    if agent_id and refused is not None:
        return ApiError(
            status_code=422,
            code=ErrorCode.INVALID_AGENT_CONFIG,
            message=f"agent {agent_id} cannot run: {refused.describe()}",
            details={"reason": refused.reason, "field": refused.field},
        )
    if refused is not None:
        return ApiError(
            status_code=502,
            code=ErrorCode.INVALID_AGENT_CONFIG,
            message=f"server_url returned an agent that cannot run: {refused.reason}",
            details={"reason": refused.reason, "field": refused.field},
        )
    return ApiError(
        status_code=502,
        code=ErrorCode.INTERNAL_ERROR,
        message="server_url did not return a valid agent",
    )
