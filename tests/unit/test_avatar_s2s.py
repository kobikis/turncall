"""An Avatar on a speech-to-speech Agent (#401).

The Avatar reads the agent's spoken audio, which every realtime service
produces itself, so on speech-to-speech it sits where it sits in cascade after
TTS: between the realtime service and the output, ahead of the ambience's
speech judge (`SpeechBeforeAmbience`), which reads the Avatar's own audio.
"""

import uuid
from typing import Any

import numpy as np
import pytest
from pipecat.frames.frames import (
    InterruptionFrame,
    OutputAudioRawFrame,
    SpeechOutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.llm_service import LLMService
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transports.base_output import BOT_VAD_STOP_SECS

from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.orchestrator import pipeline_factory
from turncall.orchestrator.ambience import SpeechBeforeAmbience
from turncall.orchestrator.avatar_watch import (
    WatchedHeyGenVideoService,
    WatchedTavusVideoService,
)
from turncall.orchestrator.pipeline_factory import CallContext

pytestmark = pytest.mark.unit

S2S_PROVIDERS = ["openai", "openai_live", "google", "aws", "xai"]
AVATARS = {
    "heygen": ({"avatar_id": "a1"}, WatchedHeyGenVideoService),
    "tavus": ({"replica_id": "r1"}, WatchedTavusVideoService),
}
CREDS = creds(
    "k",
    google="k",
    xai="k",
    heygen="k",
    tavus="k",
)


@pytest.fixture(autouse=True)
async def _close_shared_http_session():
    yield
    from turncall.adapters.aiohttp_client import close_aiohttp_session

    await close_aiohttp_session()


def _call_context() -> CallContext:
    return CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=None,
    )


def _processors(
    mocker: Any, config: AgentConfig, *, avatar_enabled: bool = True
) -> tuple[list[Any], Any]:
    """The S2S pipeline's processors, as a browser call builds them."""
    captured: list[Any] = []

    def _capture(processors: list[Any]) -> Any:
        captured.extend(processors)
        return mocker.MagicMock()

    transport = mocker.MagicMock()
    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    pipeline_factory.create_pipeline(
        config=config,
        transport=transport,
        call_context=_call_context(),
        credentials=CREDS,
        pipecat_settings=mocker.MagicMock(),
        audio_sample_rate=16000,
        avatar_enabled=avatar_enabled,
    )
    return captured, transport.output.return_value


def _agent(provider: str, avatar: str, **config: Any) -> AgentConfig:
    ids, _ = AVATARS[avatar]
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": provider},
            "transport": "webrtc",
            "avatar": {"enabled": True, "provider": avatar, **ids},
            # Nova Sonic resolves its credentials at build.
            "aws": {"access_key_id": "AKIA", "secret_access_key": "s"},
            **config,
        }
    )


def _index(processors: list[Any], kind: type) -> int:
    found = [i for i, p in enumerate(processors) if isinstance(p, kind)]
    assert len(found) == 1, f"expected one {kind.__name__}, found {len(found)}"
    return found[0]


@pytest.mark.parametrize("avatar", list(AVATARS))
@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_the_avatar_sits_after_the_realtime_service_and_before_the_output(
    mocker, provider, avatar
) -> None:
    processors, output = _processors(mocker, _agent(provider, avatar))
    service = _index(processors, LLMService)
    shown = _index(processors, AVATARS[avatar][1])
    assert service < shown < processors.index(output)


@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_ambience_judges_the_avatars_audio(mocker, provider) -> None:
    # The Avatar streams its leg's audio, silence included, as gpt-live-1
    # does: the judge has to see it before the output mixes the room in (#265).
    config = _agent(provider, "heygen", ambience={"sound": str(uuid.uuid4())})
    processors, output = _processors(mocker, config)
    shown = _index(processors, WatchedHeyGenVideoService)
    judge = _index(processors, SpeechBeforeAmbience)
    assert shown < judge == processors.index(output) - 1


@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_none_when_the_build_said_no(mocker, provider) -> None:
    # A phone call, or an Avatar with no id: `avatar_gate` decided.
    processors, _ = _processors(
        mocker, _agent(provider, "heygen"), avatar_enabled=False
    )
    kinds = tuple(kind for _, kind in AVATARS.values())
    assert not any(isinstance(p, kinds) for p in processors)


# --- gpt-live-1 -------------------------------------------------------------
#
# Every other realtime service hands out its reply as TTS audio, which is what
# the Avatar reads. gpt-live-1 streams speech instead, silence included, at
# real-time pace: the Avatar would read none of it, and pass the whole stream
# on beside its own leg's audio, two real-time streams into one output.

_RATE = 24000


def _stream(level: int, secs: float) -> list[SpeechOutputAudioRawFrame]:
    """gpt-live-1's stream: 20 ms frames, `level` 0 being its silence."""
    samples = np.full(_RATE // 50, level, np.int16).tobytes()
    return [
        SpeechOutputAudioRawFrame(audio=samples, sample_rate=_RATE, num_channels=1)
        for _ in range(round(secs * 50))
    ]


def _secs(frames: list[Any]) -> float:
    return sum(len(f.audio) for f in frames) / (2 * _RATE)


async def test_gpt_live_1s_reply_reaches_the_avatar_as_tts_audio() -> None:
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar

    reply = [
        *_stream(0, 1.0),  # before it speaks: nothing to say
        *_stream(3000, 0.3),
        *_stream(0, 0.2),  # a pause inside the reply keeps its length
        *_stream(3000, 0.2),
        *_stream(0, 1.0),  # after it: the reply is over
    ]
    down, _ = await run_test(SpeechForTheAvatar(), frames_to_send=reply)

    spoken = [f for f in down if isinstance(f, OutputAudioRawFrame)]
    assert all(type(f) is TTSAudioRawFrame for f in spoken)
    # The reply and its pause, plus at most pipecat's stop window of tail.
    assert 0.7 <= _secs(spoken) <= 0.7 + BOT_VAD_STOP_SECS + 0.02
    assert [type(f) for f in down if isinstance(f, TTSStoppedFrame)] == [
        TTSStoppedFrame
    ]
    assert type(down[-1]) is TTSStoppedFrame


async def test_a_barge_in_ends_the_reply_it_cut() -> None:
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar

    # Slept around: an interruption is a system frame and overtakes queued audio.
    frames = [
        *_stream(3000, 0.2),
        SleepFrame(0.05),
        InterruptionFrame(),
        SleepFrame(0.05),
        *_stream(0, 0.2),
    ]
    down, _ = await run_test(SpeechForTheAvatar(), frames_to_send=frames)
    after = down[[type(f) for f in down].index(InterruptionFrame) + 1 :]
    # The silence after the barge-in is not the rest of the reply.
    assert not [f for f in after if isinstance(f, OutputAudioRawFrame)]


async def test_tts_audio_passes_untouched() -> None:
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar

    reply = [
        TTSStartedFrame(),
        TTSAudioRawFrame(audio=b"\x00\x00" * 480, sample_rate=_RATE, num_channels=1),
        TTSStoppedFrame(),
    ]
    down, _ = await run_test(SpeechForTheAvatar(), frames_to_send=reply)
    assert [type(f) for f in down] == [type(f) for f in reply]
    assert down[1].audio == reply[1].audio


@pytest.mark.parametrize("avatar", list(AVATARS))
@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_the_avatar_reads_through_it(mocker, provider, avatar) -> None:
    from turncall.orchestrator.avatar_speech import SpeechForTheAvatar

    processors, _ = _processors(mocker, _agent(provider, avatar))
    shown = _index(processors, AVATARS[avatar][1])
    assert _index(processors, SpeechForTheAvatar) == shown - 1
