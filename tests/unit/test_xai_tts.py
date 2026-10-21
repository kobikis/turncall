"""xAI's voices as the `xai` TTS provider (#395, parent #385).

What is checked is what pipecat is handed: the HTTP service (never the
WebSocket one, which stalls after half a second of each reply), the voice, the
key and the settings. The audio itself is the live file's
(`tests/live/test_live_xai_tts.py`).
"""

import pytest
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig, TTSConfig
from turncall.orchestrator.pipeline_factory import _create_tts_service, _tts_label

pytestmark = pytest.mark.unit


def _agent(**tts) -> AgentConfig:
    return AgentConfig(system_prompt="hi", tts=TTSConfig(provider="xai", **tts))


def _service(**tts):
    return _create_tts_service(_agent(**tts), creds(xai="xai-key"))


class TestTheService:
    def test_an_agent_with_no_voice_gets_xai_http_tts_as_eve(self) -> None:
        from pipecat.services.xai.tts import XAIHttpTTSService

        svc = _service()

        assert type(svc) is XAIHttpTTSService
        assert svc._settings.voice == "eve"
        assert svc._api_key == "xai-key"
        # None: pipecat takes the transport's output rate at start.
        assert svc._init_sample_rate is None

    def test_a_chosen_voice_and_speed_are_sent(self) -> None:
        svc = _service(voice="rex", speed=1.2)
        assert svc._settings.voice == "rex"
        assert svc._settings.speed == 1.2

    def test_no_model_is_sent_whatever_the_config_says(self) -> None:
        """xAI TTS has no model; a stale one from another provider goes nowhere."""
        svc = _service(model="eleven_flash_v2_5")
        assert svc._settings.model is None
        assert "model" not in svc._settings.extra

    def test_extra_lands_on_the_settings_xai_reads(self) -> None:
        svc = _service(
            extra={
                "optimize_streaming_latency": 1,
                "text_normalization": True,
                "language": "es-MX",
            }
        )
        assert svc._settings.optimize_streaming_latency == 1
        assert svc._settings.text_normalization is True
        assert svc._settings.language == "es-MX"
        assert "optimize_streaming_latency" not in svc._settings.extra

    async def test_xais_bracket_tags_are_stripped_for_now(self) -> None:
        """xAI reads `[laugh]`, but the model writes such tags for no voice in
        particular; until there is a per-provider set they stay unspoken."""
        [text_filter] = _service()._text_filters
        assert await text_filter.filter("[laugh] That is great!") == "That is great!"

    def test_the_label_names_the_voice_not_a_missing_model(self) -> None:
        assert _tts_label(_agent()) == "xai/eve"
        assert _tts_label(_agent(voice="rex")) == "xai/rex"


class TestTheSchema:
    def test_an_agent_may_name_it(self) -> None:
        schema = AgentConfigSchema.model_validate({"tts": {"provider": "xai"}})
        assert schema.tts.provider == "xai"

    @pytest.mark.parametrize("speed", [0.7, 1.0, 1.5])
    def test_speed_within_xais_range_is_accepted(self, speed) -> None:
        schema = AgentConfigSchema.model_validate(
            {"tts": {"provider": "xai", "speed": speed}}
        )
        assert schema.tts.speed == speed

    @pytest.mark.parametrize("speed", [0.5, 1.6, 4.0])
    def test_speed_xai_would_400_is_refused(self, speed) -> None:
        with pytest.raises(ValidationError, match=r"xai.*0\.7.*1\.5"):
            AgentConfigSchema.model_validate(
                {"tts": {"provider": "xai", "speed": speed}}
            )

    def test_the_rule_holds_in_a_failover_entry(self) -> None:
        with pytest.raises(ValidationError, match=r"xai.*0\.7.*1\.5"):
            AgentConfigSchema.model_validate(
                {"tts": {"failover": [{"provider": "xai", "speed": 2.0}]}}
            )

    def test_other_providers_keep_their_range(self) -> None:
        schema = AgentConfigSchema.model_validate({"tts": {"speed": 2.0}})
        assert schema.tts.speed == 2.0


METFORMIN = {"Metformin": "mɛtˈfɔɹmɪn"}  # noqa: RUF001


class TestPronunciations:
    """xAI has no pronunciation markup: the words are spoken as written, and
    the author is told so on the agent read and at build."""

    def test_the_agent_read_warns(self) -> None:
        from turncall.orchestrator.config_warnings import config_warnings

        found = config_warnings(_agent(pronunciations=METFORMIN))

        assert [w["code"] for w in found] == ["pronunciations_unreadable"]
        assert "xai (eve) has no pronunciation markup" in found[0]["message"]

    def test_the_build_warns(self) -> None:
        from loguru import logger

        from turncall.orchestrator.pipeline_factory import _create_tts_stage

        said: list[str] = []
        sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
        try:
            _create_tts_stage(_agent(pronunciations=METFORMIN), creds(xai="k"))
        finally:
            logger.remove(sink)

        assert any(
            "xai (eve)" in m and "Metformin" in m and "spoken as written" in m
            for m in said
        ), said


class TestTheBuildHoldsTheSameRules:
    """A call-init inline agent skips the API schema; the build refuses the
    speed xAI would answer 400 to rather than lose every reply's audio."""

    @pytest.mark.parametrize("speed", [0.5, 1.6])
    def test_a_speed_out_of_range_fails_the_build(self, speed) -> None:
        with pytest.raises(ValueError, match=r"xai.*0\.7.*1\.5"):
            _service(speed=speed)

    def test_a_failover_member_fails_the_build_too(self) -> None:
        from turncall.orchestrator.pipeline_factory import _create_tts_stage

        backup = TTSConfig(provider="xai", speed=2.0)
        config = AgentConfig(system_prompt="hi", tts=TTSConfig(failover=[backup]))
        with pytest.raises(ValueError, match=r"xai.*0\.7.*1\.5"):
            _create_tts_stage(config, creds(xai="xai-key"))

    @pytest.mark.parametrize("speed", [0.7, 1.5])
    def test_the_edges_of_the_range_build(self, speed) -> None:
        assert _service(speed=speed)._settings.speed == speed
