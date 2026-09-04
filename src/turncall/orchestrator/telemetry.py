"""OpenTelemetry tracing setup + Pipecat observers. See ADR-0010.

Two mechanisms, both on by default:
- Observers: log latency/turn/LLM/transcription/startup. Attached per-call to the
  PipelineTask, so they cover cascade *and* S2S.
- Tracing: conversation→turn→STT/LLM/TTS spans exported to an OTLP backend. Set up
  once at startup; the call_id is the trace's conversation_id.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from loguru import logger

_tracing_active = False


def is_tracing_active() -> bool:
    """Whether tracing was successfully set up at startup."""
    return _tracing_active


def init_tracing(*, enabled: bool, service_name: str, is_production: bool) -> bool:
    """Set up OTel tracing once at app startup. Returns whether tracing is active.

    Production never console-exports (sync stdout I/O would stall the audio path,
    ADR-0004) and self-disables with a warning when no OTLP endpoint is set.
    """
    global _tracing_active
    _tracing_active = False
    if not enabled:
        return False

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if is_production and not endpoint:
        logger.warning(
            "otel_tracing_disabled",
            reason="production requires OTEL_EXPORTER_OTLP_ENDPOINT; "
            "not falling back to console export",
        )
        return False

    try:
        from pipecat.utils.tracing.setup import setup_tracing
    except ImportError:
        logger.warning("otel_tracing_unavailable", reason="pipecat[tracing] missing")
        return False

    exporter = _build_exporter(endpoint)
    # Console export only in dev, only when no real exporter is configured.
    console = (not is_production) and exporter is None
    if exporter is None and not console:
        return False  # prod path already handled; nothing to export to

    _tracing_active = bool(
        setup_tracing(
            service_name=service_name, exporter=exporter, console_export=console
        )
    )
    logger.info(
        "otel_tracing_initialized",
        active=_tracing_active,
        target=endpoint or ("console" if console else "none"),
    )
    return _tracing_active


def _build_exporter(endpoint: str | None) -> Any | None:
    """Build an OTLP span exporter from the standard OTEL env vars (or None)."""
    if not endpoint:
        return None
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    try:
        if protocol.startswith("grpc"):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        # Argless: the SDK reads OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS from the env.
        return OTLPSpanExporter()
    except ImportError:
        logger.warning("otel_exporter_unavailable", protocol=protocol)
        return None


def build_observers(enabled: bool) -> list[Any]:
    """The operational observers (empty list when disabled)."""
    if not enabled:
        return []
    from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
    from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
    from pipecat.observers.loggers.transcription_log_observer import (
        TranscriptionLogObserver,
    )
    from pipecat.observers.startup_timing_observer import StartupTimingObserver
    from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
    from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver

    return [
        UserBotLatencyObserver(),
        TurnTrackingObserver(),
        StartupTimingObserver(),
        LLMLogObserver(),
        TranscriptionLogObserver(),
        # Surfaces per-turn metrics incl. TTFA (Time To First Audio, new in
        # pipecat 1.5.0) alongside TTFB/processing/turn latency.
        MetricsLogObserver(),
    ]


def build_span_attributes(
    *,
    project_id: UUID,
    agent_id: UUID,
    call_sid: str | None = None,
    direction: str | None = None,
    transport: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    include_pii: bool,
) -> dict[str, str]:
    """Span attributes for a call. Phone numbers only when include_pii (ADR-0010)."""
    attrs: dict[str, str] = {
        "turncall.project_id": str(project_id),
        "turncall.agent_id": str(agent_id),
    }
    if call_sid:
        attrs["turncall.call_sid"] = call_sid
    if direction:
        attrs["turncall.direction"] = direction
    if transport:
        attrs["turncall.transport"] = transport
    if include_pii:
        if from_number:
            attrs["turncall.from_number"] = from_number
        if to_number:
            attrs["turncall.to_number"] = to_number
    return attrs
