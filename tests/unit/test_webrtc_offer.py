"""A browser's bundled offer reaches pipecat's peer with the right candidates (#398).

pipecat 1.13 answers with `bundlePolicy=MAX_BUNDLE`, so every m-line of the
offer shares one ICE transport, and aiortc fills it from each m-line in turn:
the last one's candidates win. A browser bundles on the *first* m-line and
closes the others' ports once the answer arrives, so a caller who cannot reach
the server's own address (a container behind Docker Desktop) was checked on a
port it had closed, and the call never connected.
"""

import pytest
from aiortc import RTCPeerConnection

from turncall.orchestrator.webrtc_offer import bundled_on_the_first_mline

pytestmark = pytest.mark.unit


async def _browser_offer() -> str:
    """What pipecat's JS client offers: audio, two video m-lines and a data
    channel, each gathered on its own port, candidates in the SDP."""
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    pc.addTransceiver("video", direction="sendrecv")
    pc.addTransceiver("video", direction="recvonly")
    pc.createDataChannel("rtvi")
    await pc.setLocalDescription(await pc.createOffer())
    sdp = pc.localDescription.sdp
    await pc.close()
    return sdp


def _ports_by_mline(sdp: str) -> list[set[int]]:
    ports: list[set[int]] = []
    for line in sdp.splitlines():
        if line.startswith("m="):
            ports.append(set())
        elif line.startswith("a=candidate:") and ports:
            ports[-1].add(int(line.split()[5]))
    return ports


async def _ports_pipecat_checks(sdp: str) -> set[int]:
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    connection = SmallWebRTCConnection(ice_servers=[])
    await connection.initialize(sdp=sdp, type="offer")
    try:
        ice = connection.pc.getTransceivers()[0].receiver.transport.transport
        return {c.port for c in ice.getRemoteCandidates()}
    finally:
        await connection.pc.close()


@pytest.mark.asyncio
async def test_pipecat_still_needs_it() -> None:
    """Fails once pipecat (or aiortc) reads a bundled offer's first m-line."""
    offer = await _browser_offer()
    first, *_, last = _ports_by_mline(offer)
    assert first and last and first != last

    assert await _ports_pipecat_checks(offer) == last


@pytest.mark.asyncio
async def test_pipecat_checks_the_port_the_browser_keeps() -> None:
    offer = await _browser_offer()
    first = _ports_by_mline(offer)[0]

    assert await _ports_pipecat_checks(bundled_on_the_first_mline(offer)) == first


@pytest.mark.asyncio
async def test_every_bundled_mline_carries_the_first_ones_candidates() -> None:
    offer = await _browser_offer()
    first = _ports_by_mline(offer)[0]

    assert _ports_by_mline(bundled_on_the_first_mline(offer)) == [first] * 4


def test_an_offer_without_bundle_is_left_alone() -> None:
    offer = (
        "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\n"
        "a=candidate:1 1 udp 1 10.0.0.1 5000 typ host\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:1\r\n"
        "a=candidate:2 1 udp 1 10.0.0.1 6000 typ host\r\n"
    )

    assert bundled_on_the_first_mline(offer) == offer


def test_mlines_outside_the_group_keep_their_own() -> None:
    offer = (
        "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
        "a=group:BUNDLE 1 2\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\n"
        "a=candidate:1 1 udp 1 10.0.0.1 5000 typ host\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:1\r\n"
        "a=candidate:2 1 udp 1 10.0.0.1 6000 typ host\r\n"
        "a=end-of-candidates\r\n"
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:2\r\n"
        "a=candidate:3 1 udp 1 10.0.0.1 7000 typ host\r\n"
    )

    assert _ports_by_mline(bundled_on_the_first_mline(offer)) == [
        {5000},
        {6000},
        {6000},
    ]
