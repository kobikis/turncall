"""The website demo holds a real call with no platform behind it.

A browser-side peer (aiortc, as the website's pipecat client is) calls
`turncall-demo` over WebRTC. The pipeline is the real one on the harness's
stub providers. The call must greet, hear the caller, end on `end_call` after
the goodbye and free its place — without reaching for a database or Redis,
both of which point at nothing here. A reach would raise inside one of the
many `except Exception` blocks on the call path and only be logged, so the
test fails on any error the call logs.
"""

from __future__ import annotations

import asyncio
import audioop
import fractions
import itertools
import json
import time

import httpx
import numpy as np
import pytest
from loguru import logger

from tests.harness.app import HARNESS_AGENT
from tests.harness.stubs import Script, ToolCall, install_stub_providers, tone

RATE = 48000
FRAME = RATE // 50  # 20 ms
GREETING = "Hello from the demo."
GOODBYE = "Goodbye then."


class Mic:
    """The caller's microphone: silence, or a tone after `say`."""

    def __init__(self) -> None:
        from aiortc import MediaStreamTrack

        mic = self

        class Track(MediaStreamTrack):
            kind = "audio"

            async def recv(self):  # type: ignore[override]
                return await mic.next_frame()

        self.track = Track()
        self._pending = b""
        self._pts = 0
        self._started: float | None = None

    def say(self, ms: int = 400) -> None:
        self._pending += tone(ms, RATE)

    async def next_frame(self):
        from av import AudioFrame

        if self._started is None:
            self._started = time.monotonic()
        # Paced in real time, as a microphone is.
        due = self._started + self._pts / RATE
        await asyncio.sleep(max(0.0, due - time.monotonic()))
        chunk, self._pending = self._pending[: FRAME * 2], self._pending[FRAME * 2 :]
        chunk = chunk.ljust(FRAME * 2, b"\0")
        frame = AudioFrame.from_ndarray(
            np.frombuffer(chunk, dtype=np.int16).reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        self._pts += FRAME
        return frame


class Ear:
    """What the caller hears: the times loud agent audio arrived."""

    def __init__(self) -> None:
        self.loud_at: list[float] = []

    async def listen(self, track) -> None:
        from aiortc.mediastreams import MediaStreamError

        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            pcm = frame.to_ndarray().astype(np.int16).tobytes()
            if audioop.rms(pcm, 2) > 500:
                self.loud_at.append(time.monotonic())

    def utterances(self) -> int:
        gaps = sum(1 for a, b in itertools.pairwise(self.loud_at) if b - a > 0.3)
        return gaps + 1 if self.loud_at else 0

    async def wait_for(self, count: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            quiet = self.loud_at and time.monotonic() - self.loud_at[-1] > 0.3
            if self.utterances() >= count and quiet:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"heard {self.utterances()} agent utterances, wanted {count}"
        )


@pytest.fixture
def demo_settings(monkeypatch, tmp_path):
    from turncall.config.settings import get_settings

    for key, value in {
        # Present so the missing-key check passes; the stubs never send them.
        "OPENAI_API_KEY": "demo-stub",
        "DEEPGRAM_API_KEY": "demo-stub",
        # Nothing listens at either: a demo call that reaches for one fails.
        "DATABASE_URL": "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "PIPECAT_ENABLE_TRACING": "false",
        # Host candidates are enough between two peers on one machine.
        "WEBRTC_ICE_SERVERS": "[]",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


async def test_a_demo_call_runs_and_ends_with_no_platform(
    demo_settings, monkeypatch, tmp_path
) -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    from turncall.demo.agents import load_demo_agents
    from turncall.demo.app import create_demo_app

    agents_file = tmp_path / "agents.json"
    agents_file.write_text(
        json.dumps(
            {
                "front-desk": {
                    **HARNESS_AGENT,
                    "first_message": GREETING,
                    "tools": [
                        {
                            "name": "end_call",
                            "description": "End the call after saying goodbye.",
                            "parameters_schema": {},
                        }
                    ],
                }
            }
        )
    )
    install_stub_providers(
        Script(caller=("That is all, bye.",), agent=(ToolCall("end_call"), GOODBYE)),
        monkeypatch.setattr,
    )
    errors: list[str] = []
    # TurnCall's own errors: pipecat's complaints about the stubs' settings
    # are the harness's, not the call's.
    sink = logger.add(
        lambda m: errors.append(str(m)),
        level="ERROR",
        filter=lambda record: record["name"].startswith("turncall"),
    )

    app = create_demo_app(
        load_demo_agents(agents_file, max_call_seconds=60), demo_settings
    )
    pc = RTCPeerConnection()
    mic, ear = Mic(), Ear()
    listening: list[asyncio.Task] = []
    pc.on(
        "track", lambda track: listening.append(asyncio.create_task(ear.listen(track)))
    )
    pc.addTrack(mic.track)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://demo"
    )
    try:
        await pc.setLocalDescription(await pc.createOffer())
        offer = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        answer = await http.post(
            "/connect", json={**offer, "requestData": {"demo": "front-desk"}}
        )
        assert answer.status_code == 200, answer.text
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer.json()["sdp"], type="answer")
        )
        # Held from the offer: a second visitor gets another container.
        assert (await http.get("/ready")).status_code == 503
        assert (await http.post("/connect", json=offer)).status_code == 429

        # Spoken once the browser is connected, not into the void before it.
        await ear.wait_for(1)
        mic.say()
        await ear.wait_for(2)  # the goodbye, after end_call

        assert await _until_idle(http), "the call never ended"
    finally:
        await pc.close()
        # A call left running when the test fails mid-way would hold the event
        # loop's teardown forever; the peer closing ends it.
        await _until_idle(http)
        await http.aclose()
        for task in listening:
            task.cancel()
        logger.remove(sink)

    assert errors == []


async def _until_idle(http: httpx.AsyncClient, timeout: float = 15) -> bool:
    """Whether the demo server freed its line within `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (await http.get("/ready")).status_code == 200:
            return True
        await asyncio.sleep(0.1)
    return False
