"""Telnyx's media stream edge: the WebSocket the `answer` command points at.

Telnyx sends `connected`, then `start` — the stream's `stream_id`, the call's
`call_control_id` and the caller's `media_format` — then `media` frames, and
`stop` when the call ends. This module reads that handshake, checks the stream
belongs to the call in its URL, and hands it to the shared media entry point
(ADR-0022), which picks pipecat's Telnyx serializer.

On Telnyx, unlike Twilio's `<Connect><Stream>`, a stream closing does not end
the call: the call stays up, silent, until something hangs it up. So when the
stream ends this edge hangs the call up through the Telnyx adapter — a call the
caller already hung up answers "already ended", which counts as done.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.domain.carriers import TELNYX
from turncall.orchestrator.transport_factory import CarrierStream
from turncall.webhooks.media_stream import run_media_stream

router = APIRouter()

# What pipecat's TelnyxFrameSerializer can decode.
_ENCODINGS = {"PCMU", "PCMA"}


async def _wait_for_start(websocket: WebSocket) -> dict[str, Any]:
    """Read messages until Telnyx's `start` event."""
    while True:
        data = json.loads(await websocket.receive_text())
        if data.get("event") == "start":
            return dict(data)
        if data.get("event") != "connected":
            logger.warning("telnyx_unexpected_pre_start_event", event=data.get("event"))


async def _belongs_to(call_id: UUID, stream: CarrierStream) -> bool:
    """Whether the stream is the one Telnyx opened for this call: a Telnyx call
    whose `call_control_id` is the stream's. A URL's call id alone is a guess
    away from someone else's call."""
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import call_repo

    async with get_session_factory()() as session:
        call = await call_repo.get_call_by_id(session, call_id)
    return (
        call is not None
        and call.provider == TELNYX
        and bool(stream.call_sid)
        and call.provider_call_sid == stream.call_sid
    )


async def _hang_up(call_id: UUID) -> None:
    """Hang the call up on Telnyx, on the account of the call's project."""
    from turncall.config import get_settings
    from turncall.services.telnyx_accounts import account_for_project
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import call_repo

    try:
        async with get_session_factory()() as session:
            call = await call_repo.get_call_by_id(session, call_id)
            if call is None or not call.provider_call_sid:
                return
            account = await account_for_project(
                session, call.project_id, get_settings()
            )
        if not account.api_key:
            logger.error("telnyx_hangup_no_key", call_id=str(call_id))
            return
        await account.adapter().end_call(call.provider_call_sid)
    except Exception:
        logger.exception("telnyx_hangup_failed", call_id=str(call_id))


@router.websocket("/ws/telnyx/media-stream/{call_id}")
async def telnyx_media_stream(websocket: WebSocket, call_id: UUID) -> None:
    """Telnyx media stream: handshake, the shared entry point, then hang up."""
    await websocket.accept()
    verified = False
    try:
        message = await _wait_for_start(websocket)
        start = message.get("start") or {}
        stream = CarrierStream(
            stream_id=str(message.get("stream_id") or ""),
            call_sid=str(start.get("call_control_id") or ""),
            encoding=str((start.get("media_format") or {}).get("encoding") or ""),
        )
        if not await _belongs_to(call_id, stream):
            logger.warning("telnyx_media_stream_not_this_call", call_id=str(call_id))
            await websocket.close()
            return
        verified = True
        if stream.encoding not in _ENCODINGS:
            # Decoding another codec as mu-law is noise, not a call.
            logger.error(
                "telnyx_media_stream_unsupported_encoding",
                call_id=str(call_id),
                encoding=stream.encoding,
            )
            return
        logger.info(
            "telnyx_media_stream_start",
            call_id=str(call_id),
            stream_id=stream.stream_id,
        )
        await run_media_stream(websocket, call_id, stream)
    except WebSocketDisconnect:
        logger.info("telnyx_media_stream_disconnected", call_id=str(call_id))
    except Exception:
        logger.exception("telnyx_media_stream_error", call_id=str(call_id))
    finally:
        if verified:
            await _hang_up(call_id)
