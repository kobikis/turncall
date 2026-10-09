"""Telnyx's media stream edge: the WebSocket the `answer` command points at.

Telnyx sends `connected`, then `start` — the stream's `stream_id`, the call's
`call_control_id` and the caller's `media_format` — then `media` frames, and
`stop` when the call ends. This module reads that handshake, checks the stream
belongs to the call in its URL — the call's media token in the URL, its
`call_control_id` in `start` — and hands it to the shared media entry point
(ADR-0022), which picks pipecat's Telnyx serializer.

On Telnyx, unlike Twilio's `<Connect><Stream>`, a stream closing does not end
the call: the call stays up, silent, until something hangs it up. So when the
stream ends this edge hangs the call up through the Telnyx adapter — a call the
caller already hung up answers "already ended", which counts as done — unless
the stream ended because the call is being transferred, which stops it on
purpose and keeps the caller on the line.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.domain.carriers import TELNYX
from turncall.orchestrator.transport_factory import CarrierStream
from turncall.services.media_token import media_token, media_token_valid
from turncall.webhooks.media_stream import MediaStreamRefused, run_media_stream

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


def media_stream_path(call_id: UUID) -> str:
    """The path of a call's stream, as the `answer` or dial names it to
    Telnyx: the call id, and the media token only that setup hands out."""
    return f"/ws/telnyx/media-stream/{call_id}/{media_token(call_id)}"


async def _belongs_to(call_id: UUID, token: str, stream: CarrierStream) -> bool:
    """Whether the stream is the one Telnyx opened for this call: its URL
    carries the call's media token, and it is a Telnyx call whose
    `call_control_id` is the stream's. A URL's call id alone is a guess away
    from someone else's call. Checked before the edge may hang anything up."""
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import call_repo

    if not media_token_valid(call_id, token):
        return False
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


async def _transferring(call_id: UUID) -> bool:
    """Whether the stream ended because the call is being transferred: the
    transfer stopped it, and the caller is now on with the operator, or about
    to be (`services/telnyx_transfer.py`). Call control parks the intent
    before stopping the stream and drops it if the transfer did not start."""
    from turncall.services.transfer import load_transfer_intent

    try:
        return await load_transfer_intent(call_id) is not None
    except Exception:
        logger.exception("telnyx_transfer_state_unreadable", call_id=str(call_id))
        return False


@router.websocket("/ws/telnyx/media-stream/{call_id}/{token}")
async def telnyx_media_stream(websocket: WebSocket, call_id: UUID, token: str) -> None:
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
        if not await _belongs_to(call_id, token, stream):
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
        await run_media_stream(websocket, call_id, stream, carrier=TELNYX, token=token)
    except MediaStreamRefused:
        # The call's own credentials, but not its stream (it has one, or it
        # is over): the call belongs to that stream, which hangs it up itself.
        verified = False
        with contextlib.suppress(Exception):
            await websocket.close()
    except WebSocketDisconnect:
        logger.info("telnyx_media_stream_disconnected", call_id=str(call_id))
    except Exception:
        logger.exception("telnyx_media_stream_error", call_id=str(call_id))
    finally:
        if verified and not await _transferring(call_id):
            await _hang_up(call_id)
