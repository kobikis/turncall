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
    assert telemetry.init_tracing(
        enabled=False, service_name="t", is_production=False
    ) is False


def test_tracing_prod_without_endpoint_self_disables():
    # No console fallback in prod — must stay off without an OTLP endpoint.
    assert telemetry.init_tracing(
        enabled=True, service_name="t", is_production=True
    ) is False
    assert telemetry.is_tracing_active() is False


def test_tracing_dev_without_endpoint_uses_console():
    assert telemetry.init_tracing(
        enabled=True, service_name="t", is_production=False
    ) is True
