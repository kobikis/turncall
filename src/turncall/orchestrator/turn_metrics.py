"""Turn metrics: each turn's timings, kept on the call record.

Pipecat's `UserBotLatencyObserver` already assembles, per user-to-bot cycle, the
TTFB every service reported and the user-to-bot latency — until now only a log
line. The recorder turns each cycle into one `metrics.turn` call event.

An observer rather than a processor: it attaches to the task, so one
implementation sees cascade and S2S alike (ADR-0010). The write goes off the
audio path; losing a record on teardown is acceptable, stalling audio is not
(ADR-0004). `metrics.turn` is never dispatched to webhook subscribers — the
latency summary on `call.ended` is the subscriber's view.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)

from turncall.domain.enums import CallEventType
from turncall.orchestrator.observability import _spawn

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

# Pipecat names a processor after its class (`DeepgramSTTService#0`). An S2S
# service is an LLM service by name (`GeminiLiveLLMService#0`), which is what it
# is to the caller: the stage that thinks.
_STAGE_MARKERS = (("STT", "stt"), ("TTS", "tts"), ("LLM", "llm"))


def stage_of(processor: str) -> str | None:
    """The stage a processor name belongs to, or None for anything else."""
    return next(
        (stage for marker, stage in _STAGE_MARKERS if marker in processor), None
    )


def turn_payload(
    turn: int, user_to_bot_secs: float, breakdown: LatencyBreakdown
) -> dict[str, Any]:
    """One `metrics.turn` payload. A stage absent from the turn is omitted, never
    zeroed; a stage that reported twice (an LLM round after a tool call) keeps its
    first report, which is the one the caller waited on.

    `turn_end_ms` is the part of `user_to_bot_ms` spent deciding the caller had
    finished: from their silence to the turn's release — VAD, the transcript,
    and Smart Turn's verdict or Deepgram Flux's end-of-turn (#204). Absent when
    pipecat measured none, as for a turn released before VAD heard the silence.
    """
    stages: dict[str, dict[str, Any]] = {}
    for ttfb in breakdown.ttfb:
        stage = stage_of(ttfb.processor)
        if stage and stage not in stages:
            stages[stage] = {
                "ttfb_ms": round(ttfb.duration_secs * 1000),
                "service": ttfb.processor,
            }
    turn_end = breakdown.user_turn_secs
    return {
        "turn": turn,
        "user_to_bot_ms": round(user_to_bot_secs * 1000),
        **({"turn_end_ms": round(turn_end * 1000)} if turn_end is not None else {}),
        "stages": stages,
    }


class TurnMetricsRecorder:
    """Owns a latency observer and writes one record per measured turn."""

    def __init__(
        self,
        call_context: CallContext,
        *,
        write: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._call_context = call_context
        self._write = write or self._write_event
        self._turn = 0
        self._measured: float | None = None
        self.observer = UserBotLatencyObserver(time_source=time_source)
        self.observer.add_event_handler("on_latency_measured", self._on_measured)
        self.observer.add_event_handler("on_latency_breakdown", self._on_breakdown)

    async def _on_measured(self, _observer: Any, latency_secs: float) -> None:
        # Fires only for a user turn, just before the breakdown of the same
        # cycle. The greeting's cycle has a breakdown and no measurement.
        self._measured = latency_secs

    async def _on_breakdown(self, _observer: Any, breakdown: LatencyBreakdown) -> None:
        if self._measured is None:
            return
        latency, self._measured = self._measured, None
        self._turn += 1
        _spawn(self._write(turn_payload(self._turn, latency, breakdown)))

    async def _write_event(self, payload: dict[str, Any]) -> None:
        await store_call_event(self._call_context, CallEventType.METRICS_TURN, payload)


async def store_call_event(
    ctx: CallContext, event_type: str, payload: dict[str, Any]
) -> None:
    """Store one call event and dispatch nothing: for the records only the
    call's event log carries. Best-effort — a lost record is logged."""
    try:
        from turncall.events.emit import record_call_event, redact

        # Before the session: no detector wait inside a transaction (events/emit).
        redacted = await redact(
            ctx.project_id, event_type, payload, sessions=ctx.session_factory
        )
        async with ctx.session_factory() as session:
            await record_call_event(
                session,
                call_id=ctx.call_id,
                project_id=ctx.project_id,
                event_type=event_type,
                payload=redacted,
                commit=True,
            )
    except Exception:
        logger.exception(
            "call_event_write_error", call_id=str(ctx.call_id), event_type=event_type
        )


def turn_metrics_observers(call_context: CallContext) -> list[UserBotLatencyObserver]:
    """The recorder's observer for a live call; nothing for an eval, which has
    no call row to hang the records off (ADR-0018)."""
    if not call_context.records:
        return []
    return [TurnMetricsRecorder(call_context).observer]
