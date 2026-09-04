"""Every TTS provider strips markdown before speaking (pipecat 1.5.0).

LLMs emit markdown (`**bold**`, backtick code) that TTS would otherwise read
aloud literally. `_create_tts_service` wires pipecat's `strip_markdown`
transform into all four providers. Pipecat consumes `text_transforms` as
`(aggregation_type | "*", transform)` tuples and unpacks each at runtime
(`for aggregation_type, transform in self._text_transforms`), so this guards
both the wiring AND the tuple shape — a bare callable would raise
"cannot unpack non-iterable function object" on the first spoken frame.
"""

import pytest
from pipecat.utils.text.base_text_aggregator import AggregationType

from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _create_tts_service


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs", "openai", "cartesia"])
async def test_tts_service_strips_markdown(provider, monkeypatch):
    # cartesia refuses to construct without a key; the others default to "".
    monkeypatch.setenv("CARTESIA_API_KEY", "x")
    config = AgentConfig(tts=TTSConfig(provider=provider))

    service = _create_tts_service(config, openai_api_key="x")

    # Mirror pipecat's runtime unpacking exactly — a bad shape fails here.
    applied = None
    for aggregation_type, transform in service._text_transforms:
        assert aggregation_type == "*" or isinstance(aggregation_type, AggregationType)
        result = await transform("You owe **$5** for `item-3`", AggregationType.SENTENCE)
        if result == "You owe $5 for item-3":
            applied = transform
    assert applied is not None, "strip_markdown not wired into the TTS text path"
