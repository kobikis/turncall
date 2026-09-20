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
class TestSmartTurnOwnsTheWindow:
    """The two waits are serial, so VAD must stay short when the model decides.

    `BaseSmartTurn.append_audio` is handed `is_speech=vad_user_speaking`, so
    its silence counter only starts once VAD has finished waiting. Leaving the
    agent's 800ms on VAD put end-of-turn 1.8s after the caller stopped talking
    — before the LLM was even called.
    """

    def test_vad_drops_to_pipecats_window_when_smart_turn_decides(self) -> None:
        analyzer = _build_vad_analyzer(AgentConfig(), sample_rate=8000, smart_turn=True)

        assert _params(analyzer).stop_secs == 0.2

    def test_an_agents_own_window_does_not_lengthen_it(self) -> None:
        """`silence_timeout_ms` is the turn window, and Smart Turn is the turn
        decider — so with Smart Turn on it must not also be charged to VAD."""
        analyzer = _build_vad_analyzer(
            AgentConfig(silence_timeout_ms=5000), sample_rate=8000, smart_turn=True
        )

        assert _params(analyzer).stop_secs == 0.2

    def test_turning_smart_turn_off_hands_the_window_back(self) -> None:
        """Nothing else ends the turn then, so VAD has to do it."""
        analyzer = _build_vad_analyzer(
            AgentConfig(smart_turn_detection=False),
            sample_rate=8000,
            smart_turn=True,
        )

        assert _params(analyzer).stop_secs == 0.8

    def test_s2s_keeps_the_agents_window(self) -> None:
        """S2S has no cascade turn analyzer; the call site passes no flag."""
        analyzer = _build_vad_analyzer(AgentConfig(), sample_rate=8000)

        assert _params(analyzer).stop_secs == 0.8


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
