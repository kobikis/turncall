"""The procedure window is one processor in the cascade pipeline (#442).

Every voice transport builds through `create_pipeline` (guarded by
`test_kb_pipeline_wiring.py`), so a window placed there is placed the same on
every carrier and WebRTC: there is no transport branch to miss. It sits right
before the LLM, after the user aggregator that hands the turn on, on both of
the cascade's processor lists, and reads the call's own holder.
"""

import contextlib

import pytest

from tests.conftest import creds
from turncall.domain.models import AgentConfig, VoicemailConfig
from turncall.orchestrator.procedures import CallProcedures, ProcedureWindowProcessor


def _processors(mocker, config: AgentConfig, holder: CallProcedures) -> list:
    from turncall.orchestrator import pipeline_factory

    captured: list = []

    def _capture(processors):
        captured.extend(processors)
        return mocker.MagicMock()

    call_context = mocker.MagicMock()
    call_context.is_eval = False
    call_context.procedures = holder
    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    with contextlib.suppress(Exception):
        pipeline_factory.create_pipeline(
            config=config,
            transport=mocker.MagicMock(),
            call_context=call_context,
            pipecat_settings=mocker.MagicMock(),
            credentials=creds("test-key"),
        )
    return captured


@pytest.mark.unit
@pytest.mark.parametrize("voicemail", [False, True], ids=["plain", "voicemail"])
def test_the_window_sits_right_before_the_llm(mocker, voicemail):
    from pipecat.services.llm_service import LLMService

    holder = CallProcedures()
    config = AgentConfig(voicemail_detection=VoicemailConfig(enabled=voicemail))
    processors = _processors(mocker, config, holder)

    windows = [
        i for i, p in enumerate(processors) if isinstance(p, ProcedureWindowProcessor)
    ]
    assert len(windows) == 1, processors
    assert isinstance(processors[windows[0] + 1], LLMService)


@pytest.mark.unit
def test_an_s2s_pipeline_has_no_window(mocker):
    processors = _processors(mocker, AgentConfig(pipeline_mode="s2s"), CallProcedures())

    assert not any(isinstance(p, ProcedureWindowProcessor) for p in processors)
