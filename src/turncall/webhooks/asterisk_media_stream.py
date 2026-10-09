"""Asterisk's media stream edge: the chan_websocket connection (#209).

The ARI edge (`asterisk_edge.py`) asks for an external-media channel whose id
carries the call id; Asterisk dials this route through the PBX's
websocket_client.conf entry and opens with a JSON `MEDIA_START` naming that
channel. This module reads it and hands the stream to the shared media entry
point (ADR-0022), which picks the serializer from the call's carrier.

Like Twilio's `/ws/media-stream`, the route trusts the call id it is handed:
a uuid4 nobody outside the call learns. The connection may land on any
process — nothing here needs the ARI connection's memory.
"""

import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.orchestrator.transport_factory import CarrierStream
from turncall.webhooks.media_stream import run_media_stream

router = APIRouter()

MEDIA_CHANNEL_PREFIX = "turncall-media-"
# Asterisk sends MEDIA_START as soon as it is connected.
_START_TIMEOUT_S = 5.0


def media_channel_id(call_id: UUID) -> str:
    """The external-media channel id the ARI edge creates for a call."""
    return f"{MEDIA_CHANNEL_PREFIX}{call_id}"


def call_id_of(channel_id: str) -> UUID | None:
    if not channel_id.startswith(MEDIA_CHANNEL_PREFIX):
        return None
    try:
        return UUID(channel_id.removeprefix(MEDIA_CHANNEL_PREFIX))
    except ValueError:
        return None


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
        call_id = call_id_of(str(start.get("channel_id", "")))
        if call_id is None or start.get("format") != "ulaw":
            # Not a channel the ARI edge asked for, or not in the format it
            # asked for: decoding it as ulaw would be noise.
            logger.error(
                "asterisk_media_refused",
                channel=start.get("channel"),
                format=start.get("format"),
            )
            await websocket.close()
            return
        logger.info(
            "asterisk_media_start", call_id=str(call_id), format=start.get("format")
        )
        stream = CarrierStream(stream_id=str(start.get("connection_id", "")))
        await run_media_stream(websocket, call_id, stream)
    except (WebSocketDisconnect, TimeoutError):
        logger.info("asterisk_media_disconnected")
    except Exception:
        logger.exception("asterisk_media_error")
