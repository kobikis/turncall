"""OTel tracing + observers — span attrs (PII gating), observer set, tracing
init gating by environment/endpoint. See ADR-0010.
"""

import os
from uuid import uuid4

import pytest

from turncall.orchestrator import telemetry

pytestmark = pytest.mark.unit


def test_observers_disabled_is_empty():
    assert telemetry.build_observers(False) == []


def test_observers_enabled_are_the_operational_set():
    names = [type(o).__name__ for o in telemetry.build_observers(True)]
    assert names == [
        "UserBotLatencyObserver",
        "TurnTrackingObserver",
        "StartupTimingObserver",
        "LLMLogObserver",
        "TranscriptionLogObserver",
        "MetricsLogObserver",
    ]


def test_span_attrs_without_pii_omit_numbers():
    attrs = telemetry.build_span_attributes(
        project_id=uuid4(),
        agent_id=uuid4(),
        direction="inbound",
        transport="twilio",
        from_number="+15551234567",
        to_number="+18005550000",
        include_pii=False,
    )
    assert "turncall.from_number" not in attrs
    assert "turncall.to_number" not in attrs
    assert attrs["turncall.direction"] == "inbound"
    assert attrs["turncall.transport"] == "twilio"


def test_span_attrs_with_pii_include_numbers():
    attrs = telemetry.build_span_attributes(
        project_id=uuid4(),
        agent_id=uuid4(),
        from_number="+15551234567",
        to_number="+18005550000",
        include_pii=True,
    )
    assert attrs["turncall.from_number"] == "+15551234567"
    assert attrs["turncall.to_number"] == "+18005550000"


@pytest.fixture(autouse=True)
def _clear_otel_env():
    saved = os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    yield
    telemetry._tracing_active = False
    if saved is not None:
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = saved


def test_tracing_disabled_flag():
    assert (
        telemetry.init_tracing(enabled=False, service_name="t", is_production=False)
        is False
    )


def test_tracing_prod_without_endpoint_self_disables():
    # No console fallback in prod — must stay off without an OTLP endpoint.
    assert (
        telemetry.init_tracing(enabled=True, service_name="t", is_production=True)
        is False
    )
    assert telemetry.is_tracing_active() is False


def test_tracing_dev_without_endpoint_uses_console():
    assert (
        telemetry.init_tracing(enabled=True, service_name="t", is_production=False)
        is True
    )


def test_trace_include_pii_defaults_off(monkeypatch):
    """Caller numbers reach the tracing backend only if the operator opts in
    (#315, ADR-0024)."""
    from turncall.config.settings import PipecatSettings

    monkeypatch.delenv("PIPECAT_TRACE_INCLUDE_PII", raising=False)
    assert PipecatSettings().trace_include_pii is False
    monkeypatch.setenv("PIPECAT_TRACE_INCLUDE_PII", "true")
    assert PipecatSettings().trace_include_pii is True


async def test_a_calls_spans_carry_no_numbers_by_default(monkeypatch):
    """With the default settings, a call's span attributes have no
    from_number/to_number; opting in puts them there (#315)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from turncall.config.settings import PipecatSettings
    from turncall.orchestrator import call_session
    from turncall.storage.repositories import call_repo

    @asynccontextmanager
    async def sessions():
        yield None

    async def the_call(_session, _call_id):
        return SimpleNamespace(
            direction="inbound",
            provider="twilio",
            from_number="+15550001234",
            to_number="+15550009999",
        )

    monkeypatch.setattr(call_repo, "get_call_by_id", the_call)
    monkeypatch.delenv("PIPECAT_TRACE_INCLUDE_PII", raising=False)
    settings = SimpleNamespace(pipecat=PipecatSettings())
    monkeypatch.setattr(call_session, "get_settings", lambda: settings)
    session = call_session.CallSession.__new__(call_session.CallSession)
    session._call_context = SimpleNamespace(
        project_id=uuid4(),
        agent_id=uuid4(),
        call_id=uuid4(),
        call_sid="CA1",
        records=True,
        session_factory=sessions,
    )

    _, attrs = await session._build_telemetry()
    assert "turncall.from_number" not in attrs
    assert "turncall.to_number" not in attrs
    assert attrs["turncall.direction"] == "inbound"
    assert attrs["turncall.transport"] == "twilio"

    monkeypatch.setenv("PIPECAT_TRACE_INCLUDE_PII", "true")
    settings.pipecat = PipecatSettings()
    _, attrs = await session._build_telemetry()
    assert attrs["turncall.from_number"] == "+15550001234"
    assert attrs["turncall.to_number"] == "+15550009999"
