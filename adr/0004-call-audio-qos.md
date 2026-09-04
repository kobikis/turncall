# 0004 — Call audio quality of service (no mid-call cut-outs)

Production calls showed three audible defects, on both Twilio PSTN and WebRTC:
**scrambled audio**, **bot cut off mid-word**, and **brief dead air** — all
while the connection stayed up (a soft cut, not a hard disconnect) and recovered
afterward. This ADR records the root causes found and the fixes shipped.

## Root causes

1. **Stateless resampling in the Twilio serializer.** `serializer.py` resampled
   TTS audio to 8kHz with `audioop.ratecv(audio, 2, 1, rate, 8000, None)` —
   passing `None` as filter state on **every frame**, resetting the resampler at
   each ~20ms boundary. This is the exact bug `audio_resampler.py` was written to
   fix for the S2S path; the serializer was never updated. Produces clicks /
   aliasing on continuous audio → perceived as "scrambled" and "choppy cut-outs"
   on PSTN.

2. **Blocking work on the live frame path.** The observability transcript taps
   (`observability.py`) `await`-ed a DB insert + commit + **webhook HTTP
   dispatch** inline before `push_frame`. A slow webhook or DB hiccup adds
   latency directly into the pipeline, contributing to event-loop jitter that
   starves the realtime-paced audio output → transient mid-word cut-outs that
   recover.

3. **No pipeline metrics.** `PipelineTask` was built with no `PipelineParams`, so
   there was zero timing visibility — the transient stall behind (2) could not be
   measured.

## Decisions

- **Carry ratecv state across frames** (`serializer.py`). Minimal fix: keep the
  state tuple on the serializer instance and reset only if the input rate changes
  mid-call. Kept `audioop` rather than switching to SOXR — note: `audioop` is
  removed in Python 3.13, so migrate to `create_stream_resampler` at that bump.
- **Fire-and-forget the transcript logging** (`observability.py`). Logging now
  runs in a tracked background task (`_spawn`); the frame path never blocks on
  DB/webhook I/O. A lost final transcript on call teardown is accepted.
- **Enable metrics** (`call_session.py`): `PipelineParams(enable_metrics=True,
  enable_usage_metrics=True)` so TTS / per-processor timing is observable.

## Scope / what was NOT done

- No reconnection logic was added. The codebase still has **zero** provider/
  WebSocket reconnect — a *hard* socket stall still kills a call silently (the
  blanket `except` in `call_session.py` only catches raised exceptions, not a
  hung socket). Out of scope here because the reported failure recovered; tracked
  separately.
- `max_call_duration_seconds` is still not enforced (defined, never applied).
- No WebSocket keepalive on the Twilio media stream.

## Consequences

- Twilio audio is continuous; the dominant "scrambled" complaint is addressed at
  the source.
- The hot path no longer blocks on logging I/O, reducing event-loop jitter.
- Metrics are emitted; use them to confirm whether any residual mid-word cut
  remains and whether it correlates with ONNX (VAD / Smart Turn) inference on the
  event loop.
