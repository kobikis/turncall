"""Twilio's media stream edge: the `<Stream>` WebSocket the TwiML opens.

Twilio sends `connected`, then `start` with the `<Parameter>`s the TwiML set
(only `callId`; everything else is resolved from the call record). This
module reads that handshake and hands the stream to the shared media entry
point (ADR-0022), which is where the serializer is chosen.
"""

import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from turncall.orchestrator.transport_factory import CarrierStream
from turncall.webhooks.media_stream import run_media_stream

router = APIRouter()


async def _wait_for_start_event(websocket: WebSocket) -> dict:
    """Read WebSocket messages until we get a Twilio 'start' event."""
    while True:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        if data.get("event") == "start":
            return data
        if data.get("event") == "connected":
            continue
        logger.warning("unexpected_pre_start_event", event=data.get("event"))


@router.websocket("/ws/media-stream")
async def media_stream_websocket(websocket: WebSocket) -> None:
    """Twilio Media Stream WebSocket: handshake, then the shared entry point."""
    await websocket.accept()

    try:
        start = (await _wait_for_start_event(websocket)).get("start", {})
        custom_params = start.get("customParameters", {})
        call_id = custom_params.get("callId", "") or custom_params.get("call_id", "")
        stream = CarrierStream(
            stream_id=start.get("streamSid", ""), call_sid=start.get("callSid", "")
        )
        logger.info(
            "media_stream_start: call={call_id} stream={stream_sid}",
            call_id=call_id,
            stream_sid=stream.stream_id,
        )
        if not call_id:
            logger.error("media_stream_missing_call_id")
            return

        await run_media_stream(websocket, UUID(call_id), stream)

    except WebSocketDisconnect:
        logger.info("media_stream_disconnected")
    except Exception:
        logger.exception("media_stream_error")
