# ADR-0027: Usage, cost estimates and alerting

**Status:** Accepted (not yet built)
**Date:** 2026-10-16
**Relates to:** ADR-0008 (ended reason), ADR-0010 (OTel tracing), #179 (latency
summary). Tracked in #309; the console view is turncall-builder-web#91.

## Context

A call record carries turn metrics, a latency summary, `ended_reason` and
`error.raised`, but nothing turns them into operations: no usage per provider,
no cost, no aggregates over time, no alerts. "A call went bad and I have no idea
why" is the complaint most often made about managed platforms, and TurnCall's
per-turn data could answer it if anything presented it.

## Decision

**The engine records usage per call, per provider and role:** STT seconds, LLM
input and output tokens, TTS characters, carrier minutes. It is on the call read
and in `call.ended`.

**Cost is an estimate from a [[price table]].** The engine ships list prices for
every provider and model it supports; a project overrides them with its own
negotiated rates. Every cost figure is labelled an estimate: the provider's bill
is the truth.

**Aggregates are computed when queried.** One endpoint per project or agent and
time range: calls, outcomes by `ended_reason`, latency percentiles, cost. It
reads indexed call rows; rollup tables come only when queries get slow. The
builder stores no call data and proxies this the way it proxies call records.

**Alerting goes through Prometheus.** `/metrics` gains counters labelled by
`ended_reason`, error stage and failover, and the Helm chart ships default alert
rules (error rate, p95 latency, failovers). Alert rules editable in the console
are not built: they would need an alert engine in TurnCall, and the operators who
self-host already run Prometheus. A hosted tier (#310) is when to revisit.

## Considered options

- **Aggregates in the builder.** Would make the builder store call data, which it
  deliberately does not (ADR-0005 in builder-api).
- **Alert rules as an engine feature with webhook delivery.** More for
  non-operators, but a second alerting system beside the one every Kubernetes
  install already has.
