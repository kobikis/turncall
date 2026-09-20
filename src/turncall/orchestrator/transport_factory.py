"""Transport factory — creates the appropriate Pipecat transport.

Supports:
  - "twilio": FastAPIWebsocketTransport + TwilioFrameSerializer (phone calls)
  - "webrtc": SmallWebRTCTransport (browser-based calls)
  - "eval": EvalTransport, a loopback WS server the eval harness connects to
"""

from typing import Any

from fastapi import WebSocket

from turncall.orchestrator.serializer import TwilioFrameSerializer

STUN_SERVERS = ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]

# Evals run over loopback, so there is no carrier to match: 16kHz is what the
# STT and TTS services want natively, and avoids a resample on both ends.
EVAL_SAMPLE_RATE = 16000


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


def create_eval_transport(port: int, *, audio: bool = False) -> Any:
    """Loopback WebSocket server transport for an eval run (ADR-0018).

    Pipecat's eval harness is an RTVI WebSocket *client*; the bot hosts the
    server. That is the whole coupling between the two halves, and it is cheap
    here because `create_pipeline` already takes its transport injected — so an
    eval swaps only the transport and runs the real everything else.

    Bound to 127.0.0.1: the harness is in this process, and an eval transport
    accepts unauthenticated connections that can seed LLM context and raise the
    function-call report level. It must never be reachable off the host.

    Args:
        port: Port to listen on. The caller picks a free one (the transport
            binds it lazily at pipeline start, so port 0 would leave nothing to
            hand the harness).
        audio: Whether the bot receives and sends audio. Text-mode runs still
            need `audio_in_enabled` for the transport to build its input side;
            what silences the bot is the harness's `skip_tts` query flag.
    """
    from pipecat.evals.serializer import EvalSerializer
    from pipecat.evals.transport import EvalTransport, EvalTransportParams

    return EvalTransport(
        params=EvalTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=EVAL_SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_enabled=audio,
            audio_out_sample_rate=EVAL_SAMPLE_RATE,
            audio_out_channels=1,
            serializer=EvalSerializer(),
        ),
        host="127.0.0.1",
        port=port,
    )
