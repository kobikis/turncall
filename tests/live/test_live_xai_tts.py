"""xAI's voices, the `xai` TTS provider, against xAI itself (#395).

The service TurnCall builds, run at a phone line's 8 kHz: what says the default
voice is one xAI serves, at the rate the transport asks for, with no model.

Live: needs XAI_API_KEY. Skipped without it.
"""

import pytest
from tests.conftest import creds

from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _create_tts_service

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_PHONE_RATE = 8000


async def test_one_sentence_at_8khz_is_audio(xai_key: str) -> None:
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSSpeakFrame
    from pipecat.pipeline.task import PipelineParams
    from pipecat.tests.utils import run_test

    tts = _create_tts_service(
        AgentConfig(tts=TTSConfig(provider="xai")), creds(xai=xai_key)
    )

    down, up = await run_test(
        tts,
        frames_to_send=[TTSSpeakFrame("Thanks for calling, how can I help?")],
        pipeline_params=PipelineParams(audio_out_sample_rate=_PHONE_RATE),
    )

    errors = [f.error for f in [*down, *up] if isinstance(f, ErrorFrame)]
    assert not errors, errors
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio, "no audio from xAI"
    assert {f.sample_rate for f in audio} == {_PHONE_RATE}
    # 16-bit mono: a short sentence is well over half a second of speech.
    assert sum(len(f.audio) for f in audio) > _PHONE_RATE * 2 * 0.5
