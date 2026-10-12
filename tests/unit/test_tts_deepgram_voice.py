"""A Deepgram agent carrying another provider's voice still speaks.

Switching an agent's TTS from OpenAI to Deepgram left `voice: "alloy"` behind;
Deepgram answered the WebSocket with HTTP 400 and every call failed silent.
"""

import pytest

from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _tts_model_voice


def _deepgram(**tts: str) -> AgentConfig:
    return AgentConfig(tts=TTSConfig(provider="deepgram", **tts))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tts", "voice"),
    [
        ({"model": "aura-2-thalia-en", "voice": "alloy"}, "aura-2-thalia-en"),
        ({"model": "", "voice": "alloy"}, "aura-2-helena-en"),
        ({"model": "aura-2-helena-en", "voice": "aura-2-zeus-en"}, "aura-2-zeus-en"),
    ],
)
def test_deepgram_voice(tts: dict[str, str], voice: str) -> None:
    assert _tts_model_voice(_deepgram(**tts))[1] == voice
