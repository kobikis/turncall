# 0005 — App-side call recording

Calls need an audio recording persisted to object storage with `recording_url` /
`recording_status` on the call record. The existing Twilio-side plumbing
(`/webhooks/twilio/recording` + `download_and_store_recording`) was never
triggered because the inbound TwiML uses `<Connect><Stream>` and never enables
Twilio recording. Result: no file, `recording_url` empty, `recording_status`
stuck at its DB default `none`.

## Decision

Record **app-side** with Pipecat's `AudioBufferProcessor` (`CallRecorder`),
which captures merged user+bot audio and, on call end, writes a WAV via the
object-storage adapter and sets `recording_url` + `recording_status=completed`.

Chosen over Twilio-side recording because it is **transport-agnostic** (Twilio,
WebRTC, WhatsApp), has no per-minute Twilio recording fee, needs no inbound
callback, and writes straight to the configured storage backend (`./storage`
locally, S3 in prod) — which is what operators expect.

## Two non-obvious constraints (each caused a failed attempt)

1. **Placement: just BEFORE `transport.output()`, not after.**
   `BaseOutputTransport.process_frame` routes audio frames to `_handle_frame`
   (sends them to the wire) and only `push_frame`s Start/End/Cancel/System
   frames downstream. So a processor placed *after* `transport.output()` sees no
   audio — both buffers stay empty. Placed *before* it, the recorder sees bot
   audio (`TTSAudioRawFrame`, a subclass of `OutputAudioRawFrame`) flowing from
   `tts`, and user audio (`InputAudioRawFrame`, a `SystemFrame` that STT
   re-pushes downstream).

2. **Flush is driven by `on_client_disconnected`, not by an end frame.**
   `AudioBufferProcessor` only flushes (`stop_recording` → `on_audio_data`) on
   `EndFrame`/`CancelFrame`. But the common Twilio path — the caller hangs up —
   just closes the WebSocket: `_receive_messages` exits its loop and fires
   `on_client_disconnected`; **no end frame propagates through the pipeline**.
   Without an explicit flush the recording is never written and
   `recording_status` is stuck at `in_progress`. `attach_recorder` registers an
   `on_client_disconnected` handler that calls `stop_recording()`. It is
   idempotent (guards on `_recording`), so a graceful end frame afterward is a
   no-op.

## Consequences

- `started_at` is now also stamped by the pipeline (`call_session` on
  `IN_PROGRESS`) rather than depending on the Twilio status callback.
- Recording is one WAV per call at `recordings/{call_id}.wav` in the storage
  backend; mono mix (`num_channels=1`).
- Avatar calls: recorder sits after the avatar service (assumes it forwards
  audio). S2S: user audio may be partially captured since the realtime LLM
  consumes input audio directly. Both noted for follow-up.
- `recording.ready` event fires on success.
