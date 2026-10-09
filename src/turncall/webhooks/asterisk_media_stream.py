"""Asterisk's media stream edge: the chan_websocket connection (#209).

The ARI edge (`asterisk_edge.py`) asks for an external-media channel whose id
carries the call id and its media token; Asterisk dials this route through the
PBX's websocket_client.conf entry and opens with a JSON `MEDIA_START` naming
that channel. This module reads it and hands the stream to the shared media
entry point (ADR-0022), which checks the token and picks the serializer from
the call's carrier.

The token rides in the channel id rather than in `v()` URI parameters, which
leaked across calls (asterisk#2194). A call id alone proves nothing: it is
not a secret, and without the token the route would hand the call to anyone
holding one. The connection may land on any process — nothing here needs the
ARI connection's memory.
"""

import asyncio
import contextlib
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.domain.carriers import ASTERISK
from turncall.orchestrator.transport_factory import CarrierStream
from turncall.services.media_token import media_token
from turncall.webhooks.media_stream import MediaStreamRefused, run_media_stream

router = APIRouter()

MEDIA_CHANNEL_PREFIX = "turncall-media-"
_UUID_LEN = 36
# Asterisk sends MEDIA_START as soon as it is connected.
_START_TIMEOUT_S = 5.0


def media_channel_id(call_id: UUID) -> str:
    """The external-media channel id the ARI edge creates for a call: the
    call id and its media token (116 characters, inside Asterisk's 150)."""
    return f"{MEDIA_CHANNEL_PREFIX}{call_id}-{media_token(call_id)}"


def media_channel_parts(channel_id: str) -> tuple[UUID, str] | None:
    """The call id and the token a media channel id carries, or None for one
    that is not shaped like a call's."""
    if not channel_id.startswith(MEDIA_CHANNEL_PREFIX):
        return None
    rest = channel_id.removeprefix(MEDIA_CHANNEL_PREFIX)
    call_part, token = rest[:_UUID_LEN], rest[_UUID_LEN + 1 :]
    try:
        call_id = UUID(call_part)
    except ValueError:
        return None
    return call_id, token


async def _media_start(websocket: WebSocket) -> dict[str, Any]:
    """Read until `MEDIA_START`; media before it has nowhere to go yet."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect
        text = message.get("text")
        if text is None:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            logger.warning("asterisk_media_non_json_control", message=text[:80])
            continue
        if isinstance(event, dict) and event.get("event") == "MEDIA_START":
            return event


@router.websocket("/ws/asterisk-media")
async def asterisk_media_websocket(websocket: WebSocket) -> None:
    """chan_websocket media: MEDIA_START, then the shared entry point."""
    # websocket_client.conf usually offers `protocols = media`; a client that
    # offered one hangs up on a server that picks none.
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="media" if "media" in offered else None)
    try:
        start = await asyncio.wait_for(_media_start(websocket), _START_TIMEOUT_S)
        parts = media_channel_parts(str(start.get("channel_id", "")))
        if parts is None or start.get("format") != "ulaw":
            # Not a channel the ARI edge asked for, or not in the format it
            # asked for: decoding it as ulaw would be noise.
            logger.error(
                "asterisk_media_refused",
                channel=start.get("channel"),
                format=start.get("format"),
            )
            await websocket.close()
            return
        call_id, token = parts
        logger.info(
            "asterisk_media_start", call_id=str(call_id), format=start.get("format")
        )
        stream = CarrierStream(stream_id=str(start.get("connection_id", "")))
        await run_media_stream(
            websocket, call_id, stream, carrier=ASTERISK, token=token
        )
    except MediaStreamRefused:
        with contextlib.suppress(Exception):
            await websocket.close()
    except (WebSocketDisconnect, TimeoutError):
        logger.info("asterisk_media_disconnected")
    except Exception:
        logger.exception("asterisk_media_error")
