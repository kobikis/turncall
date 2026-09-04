# 0006 — `call.ended` fires after recording AND analysis

`call.ended` is the subscriber's single, comprehensive end-of-call webhook
(transcript, duration, recording, summary, analysis — see
`call_analysis_trigger._run_post_call`). It must not go out until the data it
carries is actually ready.

Two artifacts are produced **concurrently and asynchronously** after a call ends,
on different tasks:

- **Recording** — flushed on WebSocket disconnect (`CallRecorder`), uploaded to
  storage, then `recording_url` / `recording_status` are written. See ADR-0005.
- **Analysis** — run by the post-call task triggered from the Twilio status
  callback.

Previously `_run_post_call` read `recording_url` **once, before** analysis, with
no wait — so `call.ended` routinely went out with a stale `null` recording even
though the upload finished moments later.

## Decision

`call.ended` is **gated** on both artifacts:

1. Run analysis first (it overlaps the recording flush).
2. **Poll `recording_status`** until terminal (`completed`/`failed`) or a
   timeout, then read `recording_url` (`_wait_for_recording`).
3. Build the payload (now including `recording_status`) and dispatch once.

The DB `recording_status` is the coordination point between the two tasks —
chosen over an in-process event so it's robust to task ordering and process
boundaries, and so a **failed** recording also unblocks (an event signal would
hang unless failure also fired it).

### Bounds (constants, not config)

`RECORDING_WAIT_TIMEOUT_S = 15`, `RECORDING_POLL_INTERVAL_S = 0.5`. 15s is ~5× a
realistic worst-case S3 upload, so the timeout only trips on genuine failure.

### Always fire

`call.ended` is the **mandatory** end-of-call signal, so on timeout or a `failed`
recording it fires **anyway** — attaching `recording_url` only if present, and
always reporting `recording_status` so the subscriber can distinguish
"no recording" from "still pending". Recording is best-effort; the signal is
guaranteed.

## Consequences

- Happy path: poll is already terminal after analysis → near-zero added latency.
- Worst case: `call.ended` is delayed at most ~15s past analysis, never lost.
- `call.ended` payload gains `recording_status`.
- Subscribers should treat **`call.ended`** as the end-of-call trigger (and
  `recording.ready` as an earlier recording-only signal). Note: `analysis.completed`
  in the enum is **not** emitted — the comprehensive `call.ended` supersedes it.
