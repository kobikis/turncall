"""Transport factory — creates the appropriate Pipecat transport.

Supports:
  - "twilio": FastAPIWebsocketTransport + TwilioFrameSerializer (phone calls)
  - "webrtc": SmallWebRTCTransport (browser-based calls)
"""

from typing import Any

from fastapi import WebSocket

from turncall.orchestrator.serializer import TwilioFrameSerializer

STUN_SERVERS = ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]


def create_twilio_transport(
    websocket: WebSocket,
    stream_sid: str,
) -> Any:
    """Create a Twilio Media Stream transport."""
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    serializer = TwilioFrameSerializer(stream_sid=stream_sid)

    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=8000,
            audio_in_channels=1,
            audio_out_enabled=True,
            audio_out_sample_rate=8000,
            audio_out_channels=1,
            serializer=serializer,
        ),
    )


def create_webrtc_transport() -> Any:
    """Create a WebRTC transport for browser-based calls."""
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.transport import (
        SmallWebRTCTransport,
    )

    connection = SmallWebRTCConnection(ice_servers=STUN_SERVERS)

    from pipecat.transports.base_transport import TransportParams

    return (
        SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_out_enabled=True,
                audio_out_sample_rate=16000,
                audio_out_channels=1,
            ),
        ),
        connection,
    )


def create_whatsapp_transport(connection: Any, *, video_out: bool = False) -> Any:
    """Wrap a pre-established SmallWebRTCConnection in a transport.

    Used for both WhatsApp voice and browser WebRTC (both wrap a
    SmallWebRTCConnection). Pass video_out=True for avatar calls so the
    avatar's video frames reach the browser.
    """
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    params = TransportParams(
        audio_in_enabled=True,
        audio_in_sample_rate=16000,
        audio_in_channels=1,
        audio_out_enabled=True,
        audio_out_sample_rate=16000,
        audio_out_channels=1,
    )
    if video_out:
        params.video_out_enabled = True
        params.video_out_is_live = True
        params.video_out_width = 1280
        params.video_out_height = 720
        params.video_out_bitrate = 2_000_000

    return SmallWebRTCTransport(webrtc_connection=connection, params=params)
