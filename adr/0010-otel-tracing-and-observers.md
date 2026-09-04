# 0010 — OpenTelemetry tracing + built-in observers

TurnCall runs the Pipecat pipeline with `enable_metrics=True` but **consumes none
of it** (`call_session.py:62`) — TTFB/turn/processing metrics are emitted and
dropped. There's no way to answer "why did that call feel slow?" beyond reading
logs. Pipecat ships two ready mechanisms we don't use: **built-in observers**
(log latency/turn/LLM/transcription/startup) and **OpenTelemetry tracing**
(conversation→turn→STT/LLM/TTS spans with TTFB and token attributes). This adds
both.

## Decision

### Both on, in every environment

Observers and tracing are **enabled by default in all environments**, production
included. This is a deliberate, eyes-open reversal of the instinct from
[ADR-0004](0004-call-audio-qos.md) (keep work off the audio hot path) — chosen for
always-on visibility. The hot-path risk is **contained**, not ignored:

- **No console export in production.** Span export to stdout is synchronous I/O on
  the event loop — the exact stall ADR-0004 fights. Console export is dev-only.
- **Tracing needs an OTLP endpoint in prod.** If `environment=production` and no
  OTLP endpoint is configured, tracing is **off with a warning** — it does *not*
  fall back to console.
- **Observer logs go through an async loguru sink** (`enqueue=True`) so a slow log
  write can't block the pipeline.

These mitigations are load-bearing — without them, "on in prod" would
reintroduce the mid-word audio cut-outs ADR-0004 fixed.

### The five observers

Attached via the task's `observers=[...]` (so they cover **both cascade and S2S** —
unlike the transcript taps, which are cascade-only):
`UserBotLatencyObserver`, `TurnTrackingObserver`, `StartupTimingObserver`,
`LLMLogObserver`, `TranscriptionLogObserver`. `RTVIObserver` is **out** (RTVI is a
separate gap).

These **supplement, not replace**, the existing `observability.py` transcript taps:
the taps are *product data* (transcripts → DB + webhooks); the observers are
*operational logging*. Both stay.

### Tracing wiring

- `setup_tracing(service_name="turncall", exporter=…)` once at app startup
  (lifespan), exporter built from the standard `OTEL_EXPORTER_OTLP_ENDPOINT` /
  `OTEL_EXPORTER_OTLP_PROTOCOL` env vars (OTLP/HTTP default).
- On the task: `enable_tracing=True`, `enable_turn_tracking=True`,
  `conversation_id = str(call_id)` — the call_id **is** the trace's conversation id,
  so a trace joins straight to the call record.
- `enable_metrics` (already on) feeds the spans' `metrics.ttfb` and
  `gen_ai.usage.*` attributes — the previously-wasted metrics become the payload.

### Span attributes include PII, behind a switch

`additional_span_attributes`: `project_id`, `agent_id`, `direction`, `transport`,
and **`from_number` / `to_number`**. Phone numbers are PII landing in an external
tracing backend; this is a deliberate choice for debugging convenience, gated by
`trace_include_pii` (default **on**) so a compliance-sensitive deployment redacts
them without a code change.

## Scope

**In:** the five observers, OTel tracing to OTLP, the `pipecat-ai[tracing]` extra +
OTLP exporter deps, settings + lifespan wiring, span attributes.

**Out:** persisting per-call latency to Postgres / a metrics API (the OTLP backend
is the query surface); RTVI protocol / `RTVIObserver`; any change to the transcript
taps.

## Consequences

- New deps: `pipecat-ai[tracing]` (opentelemetry-api/sdk + OTLP exporter).
- New `PipecatSettings` knobs: `enable_observers`, `enable_tracing`,
  `trace_include_pii`, `otel_service_name` (endpoint/protocol via standard OTEL env
  vars).
- Verify the Pipecat 1.4 `PipelineTask` accepts `enable_tracing` /
  `conversation_id` / `observers` / `enable_turn_tracking` / `additional_span_attributes`;
  if those moved to `PipelineWorker`, that migration is part of this work.
- Traces and observer logs cover S2S too — a partial answer to the pre-existing
  "S2S logs no transcripts" gap (via `TranscriptionLogObserver`), though S2S
  transcript *persistence* remains separate.
