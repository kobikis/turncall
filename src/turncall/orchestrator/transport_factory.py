"""Transport factory — creates the appropriate Pipecat transport.

Supports:
  - a carrier's media stream: FastAPIWebsocketTransport + that carrier's
    serializer (phone calls; ADR-0022)
  - "webrtc": SmallWebRTCTransport (browser-based calls)
  - "eval": EvalTransport, a loopback WS server the eval harness connects to
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from turncall.domain.carriers import TWILIO
from turncall.orchestrator.serializer import TwilioFrameSerializer

STUN_SERVERS = ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]

# Twilio Media Streams carry 8kHz mulaw — the carrier's rate, not a choice.
TWILIO_SAMPLE_RATE = 8000
# Browser WebRTC and WhatsApp voice (both SmallWebRTC): 16kHz is what the STT
# and TTS services want natively, and the peer connection resamples to Opus.
WEBRTC_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class CarrierStream:
    """What a carrier's media stream said about itself when it started.

    Read by the carrier's own edge from its handshake; consumed only by that
    carrier's serializer and, as the call's REST handle, by call control.
    """

    stream_id: str
    call_sid: str = ""


@dataclass(frozen=True)
class CarrierMedia:
    """A carrier's media stream: its audio rate and its wire format."""

    sample_rate: int
    serializer: Callable[[CarrierStream], Any]


# The one place a call's carrier is read past its inbound edge (ADR-0022):
# everything downstream of the serializer — pipeline, recording, ambience,
# tools — never learns which carrier it is on.
CARRIER_MEDIA: dict[str, CarrierMedia] = {
    TWILIO: CarrierMedia(
        TWILIO_SAMPLE_RATE,
        lambda stream: TwilioFrameSerializer(stream_sid=stream.stream_id),
    ),
}

# Every rate a voice transport sends audio at. Ambience renditions are made for
# exactly these (ADR-0021): the mixer silently plays nothing at any other rate,
# so a transport added with a new rate must land here or its calls go quiet.
VOICE_OUTPUT_RATES = frozenset(
    {*(m.sample_rate for m in CARRIER_MEDIA.values()), WEBRTC_SAMPLE_RATE}
)

# Evals run over loopback, so there is no carrier to match: 16kHz is what the
# STT and TTS services want natively, and avoids a resample on both ends.
EVAL_SAMPLE_RATE = 16000


def create_carrier_transport(
    carrier: str,
    websocket: WebSocket,
    stream: CarrierStream,
    *,
    mixer: Any = None,
) -> Any:
    """A carrier media-stream transport, its serializer chosen by `carrier`.

    `carrier` is the call's own (`calls.provider`) and must be a key of
    `CARRIER_MEDIA` (KeyError otherwise; the media entry point checks first).
    `mixer` is the call's ambience (ADR-0021), or None for none. Every voice
    transport takes one; the eval transport deliberately does not.
    """
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    media = CARRIER_MEDIA[carrier]
    rate = media.sample_rate

    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=rate,
            audio_in_channels=1,
            audio_out_enabled=True,
            audio_out_sample_rate=rate,
            audio_out_channels=1,
            audio_out_mixer=mixer,
            serializer=media.serializer(stream),
        ),
    )


def create_webrtc_transport(*, mixer: Any = None) -> Any:
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
                audio_in_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_in_channels=1,
                audio_out_enabled=True,
                audio_out_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_out_channels=1,
                audio_out_mixer=mixer,
            ),
        ),
        connection,
    )


def create_whatsapp_transport(
    connection: Any, *, video_out: bool = False, mixer: Any = None
) -> Any:
    """Wrap a pre-established SmallWebRTCConnection in a transport.

    Used for both WhatsApp voice and browser WebRTC (both wrap a
    SmallWebRTCConnection). Pass video_out=True for avatar calls so the
    avatar's video frames reach the browser.
    """
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    params = TransportParams(
        audio_in_enabled=True,
        audio_in_sample_rate=WEBRTC_SAMPLE_RATE,
        audio_in_channels=1,
        audio_out_enabled=True,
        audio_out_sample_rate=WEBRTC_SAMPLE_RATE,
        audio_out_channels=1,
        audio_out_mixer=mixer,
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
