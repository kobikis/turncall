"""Ambience must not keep a streamed voice speaking forever (#265).

gpt-live-1 streams its audio, silence included, and pipecat's output transport
finds the end of a reply by the silence in it — after mixing the ambience in.
So under ambience the bot never stopped speaking, and the idle guard, which
starts on BotStoppedSpeakingFrame, never armed. Driven through pipecat's real
output transport with a mixer, because that is where the bug lives.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pipecat.audio.mixers.base_audio_mixer import BaseAudioMixer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    SpeechOutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.tests.utils import run_test
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator.ambience import SpeechBeforeAmbience
from turncall.orchestrator.pipeline_factory import create_pipeline

pytestmark = pytest.mark.unit

_RATE = 16000


class _Line(BaseOutputTransport):
    """An output transport that plays in real time, like a call."""

    async def start(self, frame):  # type: ignore[no-untyped-def]
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.02)
        return True


class _Room(BaseAudioMixer):
    """A constant hum: the ambience at its quietest is still not silence."""

    async def start(self, sample_rate: int) -> None: ...
    async def stop(self) -> None: ...
    async def process_frame(self, frame) -> None: ...  # type: ignore[no-untyped-def]

    async def mix(self, audio: bytes) -> bytes:
        hum = np.frombuffer(audio, dtype=np.int16).astype(np.int32) + 150
        return np.clip(hum, -32768, 32767).astype(np.int16).tobytes()


def _voice(level: int, secs: float) -> list[SpeechOutputAudioRawFrame]:
    """gpt-live-1's stream: 20 ms frames, `level` 0 being its silence."""
    samples = np.full(_RATE // 50, level, np.int16).tobytes()
    return [
        SpeechOutputAudioRawFrame(audio=samples, sample_rate=_RATE, num_channels=1)
        for _ in range(round(secs * 50))
    ]


async def _speaking(*processors: object) -> list[type]:
    line = _Line(
        TransportParams(
            audio_out_enabled=True, audio_out_sample_rate=_RATE, audio_out_mixer=_Room()
        )
    )
    reply = [*_voice(3000, 0.3), *_voice(0, 0.2), *_voice(3000, 0.2), *_voice(0, 0.6)]
    _, up = await run_test(Pipeline([*processors, line]), frames_to_send=reply)
    edges = (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)
    return [type(f) for f in up if isinstance(f, edges)]


async def test_a_pause_ends_the_reply_under_ambience() -> None:
    """One reply with a comma in it: started once, stopped once, after it."""
    assert await _speaking(SpeechBeforeAmbience()) == [
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
    ]


async def test_pipecat_still_needs_it() -> None:
    """The bug, pinned: when pipecat judges silence before mixing, this fails
    and `SpeechBeforeAmbience` can go."""
    assert await _speaking() == [BotStartedSpeakingFrame]


_OPENAI_LIVE = {"pipeline_mode": "s2s", "s2s": {"provider": "openai_live"}}
_AMBIENCE = {"ambience": {"sound": str(uuid.uuid4())}}


def _processors(config: dict, *, is_eval: bool = False) -> tuple[list, object]:
    transport = MagicMock()
    with patch("turncall.orchestrator.call_recorder.attach_recorder"):
        pipeline = create_pipeline(
            config=AgentConfig.model_validate(config),
            transport=transport,
            call_context=MagicMock(is_eval=is_eval),
            pipecat_settings=SimpleNamespace(),
            credentials=creds("sk-test"),
        )
    return pipeline.processors, transport.output.return_value


# Cascade too: a video avatar streams its audio the same way.
@pytest.mark.parametrize("agent", [_OPENAI_LIVE, {}], ids=["openai_live", "cascade"])
def test_it_sits_right_before_the_output_where_ambience_plays(agent: dict) -> None:
    processors, output = _processors({**agent, **_AMBIENCE})
    before = processors[processors.index(output) - 1]
    assert isinstance(before, SpeechBeforeAmbience)


@pytest.mark.parametrize(
    ("config", "is_eval"),
    [(_OPENAI_LIVE, False), ({**_OPENAI_LIVE, **_AMBIENCE}, True)],
    ids=["no_ambience", "eval"],
)
def test_none_where_no_ambience_plays(config: dict, is_eval: bool) -> None:
    processors, _ = _processors(config, is_eval=is_eval)
    assert not any(isinstance(p, SpeechBeforeAmbience) for p in processors)
