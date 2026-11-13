"""`stt.keyterms` on an OpenAI Realtime agent, and the session's truncation (#455).

pipecat 1.16 sends Realtime's input transcription `keywords`, which only
`gpt-transcribe` and `gpt-live-transcribe` take — not pipecat's default
`gpt-realtime-whisper`. Every other S2S provider still has nowhere to put the
terms, and its agent read says so. Seam 1 is the session a built pipeline
sends, seam 2 the warnings an agent read derives.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from loguru import logger

from tests.conftest import creds
from turncall.domain.models import AgentConfig, AWSConfig
from turncall.orchestrator.config_warnings import config_warnings
from turncall.orchestrator.pipeline_factory import create_pipeline

pytestmark = pytest.mark.unit


def _agent(provider: str = "openai", keyterms: list[str] | None = None) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": provider},
            "stt": {"keyterms": keyterms or []},
        }
    ).model_copy(
        update={"aws": AWSConfig(access_key_id="AKIATEST", secret_access_key="s")}
    )


def _session_update(config: AgentConfig) -> dict[str, Any]:
    """The `session` of the session.update the built Realtime service sends."""
    from pipecat.services.llm_service import LLMService
    from pipecat.services.openai.realtime.events import SessionUpdateEvent

    pipeline = create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=MagicMock(is_eval=True),
        pipecat_settings=SimpleNamespace(),
        credentials=creds("sk-test"),
    )
    service = next(
        p for p in pipeline.processors_with_metrics() if isinstance(p, LLMService)
    )
    session = service._settings.session_properties
    return SessionUpdateEvent(session=session).model_dump(exclude_none=True)["session"]


def _transcription(config: AgentConfig) -> dict[str, Any]:
    return _session_update(config)["audio"]["input"]["transcription"]


class TestKeytermsOnRealtime:
    def test_keyterms_are_sent_as_transcription_keywords(self) -> None:
        sent = _transcription(_agent(keyterms=["Metformin", "Nguyen"]))

        assert sent["keywords"] == ["Metformin", "Nguyen"]
        # gpt-realtime-whisper takes no keywords.
        assert sent["model"] == "gpt-transcribe"

    def test_without_keyterms_the_transcription_is_pipecats_default(self) -> None:
        sent = _transcription(_agent())

        assert "keywords" not in sent
        assert sent["model"] == "gpt-realtime-whisper"

    def test_a_keyword_openai_would_refuse_is_dropped_with_a_warning(self) -> None:
        said: list[str] = []
        sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
        try:
            sent = _transcription(_agent(keyterms=["Acme", "a<b>", "two\nlines", "  "]))
        finally:
            logger.remove(sink)

        # OpenAI rejects the whole session update over one of these.
        assert sent["keywords"] == ["Acme"]
        assert any("openai_realtime_keywords_dropped" in line for line in said)

    def test_only_unsendable_keyterms_leave_the_default_transcription(self) -> None:
        sent = _transcription(_agent(keyterms=["a<b"]))

        assert "keywords" not in sent
        assert sent["model"] == "gpt-realtime-whisper"


def test_truncation_is_left_to_the_server() -> None:
    """A long call keeps talking: no `truncation` is sent, so the server's
    default (`auto`, the oldest messages dropped as needed) applies, rather
    than `disabled`, which would answer an error once the call outgrew it."""
    assert "truncation" not in _session_update(_agent(keyterms=["Acme"]))


class TestKeytermsIgnoredWarning:
    @pytest.mark.parametrize("provider", ["google", "aws", "xai", "openai_live"])
    def test_an_s2s_provider_without_keywords_says_so(self, provider: str) -> None:
        [warning] = config_warnings(_agent(provider, keyterms=["Acme", "Nguyen"]))

        assert warning["code"] == "keyterms_ignored_on_s2s"
        assert warning["keyterms"] == ["Acme", "Nguyen"]
        assert warning["provider"] == provider
        assert provider in warning["message"]

    def test_realtime_cascade_and_no_keyterms_say_nothing(self) -> None:
        cascade = AgentConfig.model_validate({"stt": {"keyterms": ["Acme"]}})

        assert config_warnings(_agent("openai", keyterms=["Acme"])) == []
        assert config_warnings(cascade) == []
        assert config_warnings(_agent("google")) == []
