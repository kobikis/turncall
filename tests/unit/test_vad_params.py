"""silence_timeout_ms and the VAD confidence threshold must reach the analyzer.

Both were declared and read by nothing: the analyzer was built bare, so
Pipecat's defaults decided how long a pause ends a turn and how sure the
detector has to be. `silence_timeout_ms` is in the public OpenAPI spec and the
agents guide, which made it a promise rather than a leftover.
"""

from types import SimpleNamespace

import pytest

from turncall.domain.models import AgentConfig
from turncall.orchestrator.pipeline_factory import _build_vad_analyzer


def _params(analyzer):
    return analyzer.params if hasattr(analyzer, "params") else analyzer._params


@pytest.mark.unit
class TestSilenceTimeout:
    def test_it_becomes_the_vad_stop_window(self) -> None:
        """Milliseconds in the config, seconds in Pipecat."""
        analyzer = _build_vad_analyzer(
            AgentConfig(silence_timeout_ms=1500), sample_rate=8000
        )

        assert _params(analyzer).stop_secs == 1.5

    def test_the_default_is_carried_through_rather_than_left_to_pipecat(self) -> None:
        analyzer = _build_vad_analyzer(AgentConfig(), sample_rate=8000)

        assert _params(analyzer).stop_secs == 0.8

    @pytest.mark.parametrize("ms", [200, 5000])
    def test_the_validated_bounds_both_arrive(self, ms: int) -> None:
        analyzer = _build_vad_analyzer(
            AgentConfig(silence_timeout_ms=ms), sample_rate=8000
        )

        assert _params(analyzer).stop_secs == ms / 1000


@pytest.mark.unit
class TestConfidenceThreshold:
    def test_the_platform_setting_reaches_the_analyzer(self) -> None:
        analyzer = _build_vad_analyzer(
            AgentConfig(),
            sample_rate=8000,
            pipecat_settings=SimpleNamespace(vad_confidence_threshold=0.9),
        )

        assert _params(analyzer).confidence == 0.9

    def test_without_settings_pipecat_keeps_its_own_default(self) -> None:
        """The S2S path builds the analyzer without platform settings, so an
        absent threshold must leave Pipecat's default alone rather than sending
        None into VADParams."""
        analyzer = _build_vad_analyzer(AgentConfig(), sample_rate=8000)

        assert _params(analyzer).confidence is not None


@pytest.mark.unit
def test_the_sample_rate_still_gets_through() -> None:
    """Twilio is 8k and WebRTC 16k; the wrong one is silent-audio territory.

    `_init_sample_rate`, not `sample_rate` — the latter stays 0 until the
    pipeline calls set_sample_rate at start."""
    analyzer = _build_vad_analyzer(AgentConfig(), sample_rate=16000)

    assert analyzer._init_sample_rate == 16000
