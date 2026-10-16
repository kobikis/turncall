# ADR-0023: Caller-ear latency, measured over PSTN and published as measured

**Status:** Accepted (not yet built)
**Date:** 2026-10-16
**Relates to:** ADR-0005 (call recording), ADR-0010 (OTel tracing), #176, #179,
#184, #200, #204. Tracked in #305.

## Context

Every latency figure TurnCall has is measured inside the pipeline: per-stage
TTFB and `turn_end_ms` in [[turn metrics]], and the p50/p95 latency summary on
each call. None of them is what a caller hears. The site quotes ~800 ms of
processing and a 1.5–2 s wait from the caller's last word to the agent's first.

The only independent platform benchmark (Cekura, fixed stack over PSTN, Jul–Aug
2026) does not include TurnCall. It puts Pipecat at 3.15 s p50 and 5.42 s p95,
the slowest of six platforms, and buyers apply that row to anything built on
Pipecat. Capacity is already measured (#200: two calls per dedicated core);
latency at the caller's ear is not.

## Decision

**Measure end of caller speech to first agent audio in the call recording.** A
second TurnCall agent dials the agent under test over a real carrier and plays
scripted caller turns. The callee's stereo recording (one channel per party)
gives both edges of every gap, so nothing depends on clocks in two places or on
the pipeline's own account of itself. Calls run over Telnyx and Twilio.

**Three configurations:**

- the shipped defaults (Nova-3, Silero + Smart Turn, `silence_timeout_ms` 800);
- a tuned cascade: Deepgram Flux with eager end-of-turn, plus `llm.warm_up`;
- Gemini Live, the cheapest of the four speech-to-speech providers.

**The defaults follow the measurement.** If the tuned cascade is clearly faster
and the same eval scenarios pass at the same rate, it becomes the default before
the public release, so the published number is the out-of-the-box number.

**Publish what was measured.** p50 and p95 per configuration, with stack, region
and carrier stated, and the harness in the repo so anyone can rerun it. A poor
result is fixed before release, not hidden or flattered by choosing
configurations after the fact. After release, ask Cekura to add TurnCall to its
fixed-stack benchmark so a third-party number stands next to ours.

## Considered options

- **Publish the pipeline-side latency summary.** Already available, but it
  starts the clock after endpointing and ignores the carrier leg: it is not the
  number Cekura or a caller measures.
- **A synthetic caller over WebSocket** (the #200 harness). Repeatable and free,
  but skips the carrier, which is part of what a caller waits for.

## Consequences

- The harness lives beside `scripts/calls_per_pod/` and costs real carrier and
  provider minutes per run, so it runs before releases, not in CI.
- The result has to be published with the release; the repo being private until
  then means nobody else can check it before.
