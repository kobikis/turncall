"""The recorder sits after `transport.output()`, where bot audio is real-time.

Before it, the recorder saw TTS audio as the service produced it — in bursts,
far faster than it is played. `AudioBufferProcessor` keeps the two channels in
step by padding one to the other, so every burst advanced the caller's channel
too, on top of the caller audio that kept arriving in real time. A 203s
Twilio call recorded as 300s, drifting later with every agent reply (+4s,
+10s, +22s against the transcript): the recording and the transcript offsets
stopped agreeing, and the stereo tracks no longer lined up (#178).

ADR-0005 put it before the output because the pipecat of the time pushed no
audio past `transport.output()`. Pipecat 1.12 pushes each audio frame
downstream once it has been written to the wire, which is paced.
"""

import contextlib

import pytest

from tests.conftest import creds
from turncall.orchestrator.call_recorder import CallRecorder


def _processors(mocker, pipeline_mode: str) -> tuple[list, object]:
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator import pipeline_factory

    captured: list = []

    def _capture(processors):
        captured.extend(processors)
        return mocker.MagicMock()

    call_context = mocker.MagicMock()
    call_context.is_eval = False
    transport = mocker.MagicMock()
    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    with contextlib.suppress(Exception):
        pipeline_factory.create_pipeline(
            config=AgentConfig(pipeline_mode=pipeline_mode),
            transport=transport,
            call_context=call_context,
            pipecat_settings=mocker.MagicMock(),
            credentials=creds("test-key"),
        )
    return captured, transport.output.return_value


@pytest.mark.unit
@pytest.mark.parametrize("pipeline_mode", ["cascade", "s2s"])
def test_recorder_is_directly_after_the_output_transport(mocker, pipeline_mode):
    processors, output = _processors(mocker, pipeline_mode)
    recorders = [i for i, p in enumerate(processors) if isinstance(p, CallRecorder)]
    assert len(recorders) == 1, f"{pipeline_mode}: expected one recorder"
    assert processors[recorders[0] - 1] is output, (
        f"{pipeline_mode}: the recorder must follow transport.output(); before "
        "it, bursty TTS audio stretches the recording's timeline"
    )
