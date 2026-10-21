"""Transport factory — creates the appropriate Pipecat transport.

Supports:
  - a carrier's media stream: FastAPIWebsocketTransport + that carrier's
    serializer (phone calls; ADR-0022)
  - "webrtc": SmallWebRTCTransport (browser-based calls)
  - "eval": EvalFastAPIWebsocketTransport, over the loopback socket the eval
    harness opens (`evals.bot_server`)
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from turncall.domain.carriers import ASTERISK, TELNYX, TWILIO
from turncall.orchestrator.asterisk_serializer import (
    ASTERISK_SAMPLE_RATE,
    AsteriskFrameSerializer,
)
from turncall.orchestrator.serializer import TwilioFrameSerializer


def ice_servers() -> list[Any]:
    """The ICE servers a WebRTC peer connection of ours gathers with
    (`WEBRTC_ICE_SERVERS`; public STUN unless set), as pipecat's `IceServer`."""
    from pipecat.transports.smallwebrtc.connection import IceServer

    from turncall.config import get_settings

    return [
        IceServer(urls=s.urls, username=s.username, credential=s.credential)
        for s in get_settings().webrtc.ice_servers
    ]


# Twilio Media Streams carry 8kHz mulaw — the carrier's rate, not a choice.
TWILIO_SAMPLE_RATE = 8000
# Telnyx media streams: 8kHz PCMU, which the answer command asks for both ways.
TELNYX_SAMPLE_RATE = 8000
# Browser WebRTC and WhatsApp voice (both SmallWebRTC): 16kHz is what the STT
# and TTS services want natively, and the peer connection resamples to Opus.
WEBRTC_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class CarrierStream:
    """What a carrier's media stream said about itself when it started.

    Read by the carrier's own edge from its handshake; consumed only by that
    carrier's serializer and, as the call's REST handle, by call control.
    `encoding` is the caller's audio as the stream declared it, for a carrier
    whose stream says (Telnyx's `media_format.encoding`).
    """

    stream_id: str
    call_sid: str = ""
    encoding: str = ""


def _telnyx_serializer(stream: CarrierStream) -> Any:
    """Pipecat's Telnyx serializer, which keeps resampler state (ADR-0022).

    Its own hang-up is off: hanging up is a Telnyx REST command, and those
    belong to the Telnyx adapter (`webhooks/telnyx_media_stream.py` sends it
    when the stream ends). Pipecat names encodings from Telnyx's side:
    `outbound_encoding` is what Telnyx sends us, `inbound_encoding` what we
    send it — the codec the answer command asked for.
    """
    from pipecat.serializers.telnyx import TelnyxFrameSerializer

    from turncall.adapters.telephony.telnyx_adapter import STREAM_CODEC

    return TelnyxFrameSerializer(
        stream_id=stream.stream_id,
        outbound_encoding=stream.encoding or STREAM_CODEC,
        inbound_encoding=STREAM_CODEC,
        call_control_id=stream.call_sid,
        params=TelnyxFrameSerializer.InputParams(auto_hang_up=False),
    )


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
    TELNYX: CarrierMedia(TELNYX_SAMPLE_RATE, _telnyx_serializer),
    # chan_websocket external media, asked for as ulaw (#209): the PBX's own
    # G.711 rate, and one VOICE_OUTPUT_RATES already renders ambience at.
    ASTERISK: CarrierMedia(ASTERISK_SAMPLE_RATE, lambda _: AsteriskFrameSerializer()),
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

    connection = SmallWebRTCConnection(ice_servers=ice_servers())

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


def create_eval_transport(websocket: Any, flags: Any, *, audio: bool = False) -> Any:
    """The eval transport over the harness's accepted socket (ADR-0018, #375).

    Pipecat's eval harness is an RTVI WebSocket *client*. That is the whole
    coupling between the two halves, and it is cheap here because
    `create_pipeline` already takes its transport injected — so an eval swaps
    only the transport and runs the real everything else. The socket comes from
    `evals.bot_server`, which binds it to 127.0.0.1: an eval connection is
    unauthenticated and can seed LLM context, so it is never reachable off the
    host.

    Args:
        websocket: The accepted WebSocket, its `eval-connect` message read.
        flags: That message's `EvalConnectionFlags`. `skip_tts` is what
            silences the bot in a text-mode run; the transport applies it
            before the first message.
        audio: Whether the bot sends audio. Text-mode runs still need
            `audio_in_enabled` for the transport to build its input side.
    """
    from pipecat.evals.fastapi_transport import EvalFastAPIWebsocketTransport
    from pipecat.evals.serializer import EvalSerializer
    from pipecat.evals.transport import EvalTransportParams

    return EvalFastAPIWebsocketTransport(
        websocket,
        params=EvalTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=EVAL_SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_enabled=audio,
            audio_out_sample_rate=EVAL_SAMPLE_RATE,
            audio_out_channels=1,
            serializer=EvalSerializer(),
        ),
        flags=flags,
    )
