"""An opened call becomes what running takes (#494).

`open_call` answers an opened call; `run_call` takes a `CallToRun`. An edge
turns one into the other with `CallToRun.of`, so no edge re-assembles the
agent, its id or its handles from loose fields.
"""

from __future__ import annotations

import uuid

import pytest

from tests.accepted import accepted
from turncall.domain.carriers import DEFAULT_CARRIER, TWILIO
from turncall.orchestrator.call_runner import CallToRun
from turncall.services.call_opening import (
    CallMetadata,
    CallParties,
    EdgeHandles,
    OpenedCall,
)

pytestmark = pytest.mark.unit


def _opened(handles: EdgeHandles, **metadata) -> OpenedCall:
    return OpenedCall(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent=accepted({"system_prompt": "Hello {{name}}."}),
        agent_name=None,
        metadata=CallMetadata(**metadata),
        parties=CallParties(from_number="", to_number=""),
        handles=handles,
    )


def test_the_call_runs_its_agent_with_the_calls_variables_and_context() -> None:
    opened = _opened(
        EdgeHandles(carrier=DEFAULT_CARRIER, transport="webrtc"),
        template_variables={"name": "Ada"},
        knowledge_context="Ticket #456 is open.",
    )
    factory = object()

    call = CallToRun.of(opened, session_factory=factory)

    prompt = call.agent.config.system_prompt
    assert "Hello Ada." in prompt and "Ticket #456 is open." in prompt
    assert (call.call_id, call.project_id) == (opened.call_id, opened.project_id)
    # An inline agent: no agent row.
    assert call.agent_id is None
    assert call.session_factory is factory


def test_a_browser_call_is_named_by_its_transport() -> None:
    call = CallToRun.of(
        _opened(EdgeHandles(carrier=DEFAULT_CARRIER, transport="webrtc")),
        session_factory=None,
    )

    assert (call.call_sid, call.stream_sid) == ("webrtc", "webrtc")


def test_a_whatsapp_call_is_named_by_its_whatsapp_call_id() -> None:
    handles = EdgeHandles(
        carrier=DEFAULT_CARRIER, transport="whatsapp", transport_call_id="wa-1"
    )

    call = CallToRun.of(_opened(handles), session_factory=None)

    assert (call.call_sid, call.stream_sid) == ("whatsapp:wa-1", "whatsapp:wa-1")
    # Never stored as the carrier's sid: a hang-up would act on it.
    assert handles.provider_call_sid is None
    assert handles.provider_call_id == "wa-1"


def test_a_carrier_call_is_named_by_its_sid_and_its_stream() -> None:
    handles = EdgeHandles(carrier=TWILIO, provider_call_sid="CA1")

    call = CallToRun.of(_opened(handles), session_factory=None, stream_sid="MZ1")

    assert (call.call_sid, call.stream_sid) == ("CA1", "MZ1")
    assert handles.transport_name == TWILIO
