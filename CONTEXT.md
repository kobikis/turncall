# TurnCall

Glossary of domain terms specific to TurnCall — the production voice agent platform.
Definitions only; implementation lives in code and ADRs.

## LLM Providers

**Provider**:
The named LLM (or STT/TTS) backend selected per agent — e.g. `openai`, `anthropic`,
`ollama`, `custom_openai`, `openrouter`, `bedrock`. A provider determines which service
the orchestrator instantiates and how its credentials are resolved — a single API key
for all but [[Bedrock]], which resolves an AWS credential triple plus a region.
A provider names a *vendor* in every case except [[Bedrock]], which names a gateway
hosting other vendors' models.

**OpenRouter**:
A first-class LLM provider that routes to many upstream models through a single
OpenAI-compatible endpoint and platform-level key. Chosen over the generic
`custom_openai` escape hatch specifically to expose [[fallback_models]]. See ADR-0003.
_Avoid_: "custom OpenAI endpoint" (that's the separate `custom_openai` provider).

**custom_openai**:
The generic Bring-Your-Own-Model provider for any OpenAI-compatible endpoint, configured
with an arbitrary `base_url` and per-agent key. The unergonomic escape hatch; `openrouter`
is the polished, opinionated alternative for the OpenRouter case.

**Fallback models** (`fallback_models`):
An ordered list of models OpenRouter tries after the primary `model`, failing over when
the primary rate-limits or errors mid-call. Sent as OpenRouter's request-body `models`
array, primary first. Only meaningful on the `openrouter` provider. Voice pipeline only.
_Avoid_: "backup model", "retry model", "model routing".

**S2S gateway mode**:
Running the `openai` S2S [[provider]] with an `s2s.base_url` that routes the
OpenAI-Realtime WebSocket protocol to a compatible gateway (Vercel AI Gateway,
LiteLLM) or xAI direct — how third-party realtime models like Grok voice run
without their own provider. The platform `OPENAI_API_KEY` is the bearer sent to
the gateway. _Avoid_: "xai provider" — no such provider exists; the example's
`--provider xai` flag is a preset for this mode.

**Bedrock**:
The AWS-hosted gateway [[provider]] (`bedrock`) reaching Anthropic, Meta, Mistral and
Amazon foundation models through one API. The only [[provider]] whose name is a gateway
rather than a vendor: `provider: "bedrock", model: "anthropic.claude-..."` names two
different companies, and the same Claude model is reachable through either `anthropic`
or `bedrock` with different credentials and a different failure surface. Model ids come
in three forms — direct, `us.`-prefixed cross-region inference profiles, and
provisioned-throughput ARNs. See ADR-0016.
_Avoid_: "the AWS provider" — that is [[Nova Sonic]]'s `aws` S2S provider, a different thing.

**Nova Sonic**:
Amazon's native speech-to-speech model, run as the `aws` S2S [[provider]]
(`amazon.nova-2-sonic-v1:0`, i.e. Nova Sonic 2). Reached through a different API surface
than [[Bedrock]]'s converse endpoint, which is why it carries a separate provider name
rather than being a Bedrock model id. Its sessions expire at roughly six minutes and roll
over transparently — ordinary phone calls exercise that path. See ADR-0016.

## Call quality of service

**Soft cut** (vs **hard disconnect**):
A degradation where the call/transport stays connected but audio breaks —
scrambled audio, the bot stopping mid-word, or brief dead air — and typically
recovers. Distinct from a **hard disconnect**, where the session drops entirely
(ICE failure, hangup, a raised pipeline exception → `CallStatus.FAILED`). The two
have unrelated causes; name which one is meant. See ADR-0004.

**Scrambled audio**:
Clicks / aliasing on continuous audio caused by resetting the resampler filter at
every frame boundary (stateless [[resampler state]]). A [[soft cut]] symptom,
PSTN-specific (the Twilio serializer path). _Avoid_: "garbled", "choppy" as
root-cause labels — those are also produced by output underrun, a different cause.

**Resampler state**:
The filter state `audioop.ratecv` (and SOXR stream resamplers) carry between
chunks to stay continuous across frame boundaries. Must be threaded call-to-call;
passing `None` each frame resets it and produces [[scrambled audio]].

**Output underrun**:
Transient dead air when TTS audio frames arrive later than the transport plays
them — Twilio paces audio in hard realtime, so a late frame becomes a gap, then
resumes. Caused by event-loop jitter (blocking I/O or CPU work on the loop), not
by the resampler. The mechanism behind a recovering mid-word [[soft cut]].

## Call recording

**Call recording** (app-side):
The WAV written to object storage for a call, captured in-pipeline by
`CallRecorder` (a Pipecat `AudioBufferProcessor`) — NOT Twilio's recording API.
Stored at `recordings/{call_id}.wav`; the call row carries [[recording status]]
and `recording_url`. See ADR-0005. _Avoid_: "Twilio recording" (that path exists
in code but is never enabled).

**Recording status** (`recording_status`):
Lifecycle of a call's recording: `none` (default, never started) → `in_progress`
(capture started, set on StartFrame) → `completed` (WAV written) / `failed`. A
value stuck at `in_progress` means the [[recording flush]] never ran.

**Recording flush**:
The `stop_recording` → `on_audio_data` step that writes the buffered audio. It
fires on EndFrame/CancelFrame OR `on_client_disconnected`. The latter is
essential: a Twilio hangup closes the WebSocket without sending any end frame
through the pipeline, so without the disconnect hook the recording is never
written.

**`call.ended`** (the event):
The single, comprehensive end-of-call webhook — transcript, duration, recording,
summary, analysis. Dispatched by post-call processing once analysis is done AND
the [[recording flush]] has reached a terminal [[recording status]] (or a 15s
bound elapsed). The subscriber's canonical end-of-call signal; fires exactly
once, never blocked by a failed recording. See ADR-0006. _Avoid_: relying on
`analysis.completed` (defined in the enum but never emitted) — `call.ended`
supersedes it.

## Webhook events

**Event envelope** (vs **payload**):
The fixed outer fields on every delivered webhook — `event`, `project_id`,
`call_id`, `session_id`, [[agent_id (on events)]], [[event_id]], `timestamp` —
wrapping a per-event-type `payload`. Identity and routing live in the envelope;
only event-specific data lives in the payload. _Avoid_: "headers" (those are the
HTTP `X-TurnCall-*` headers, a separate thing).

**event_id**:
A fresh `uuid4` minted once per *logical* event at dispatch — stable across the
up-to-5 delivery retries and shared across all subscribers, so consumers can
dedupe redeliveries. NOT the `CallEventRow.id` and NOT regenerated per HTTP
attempt. See ADR-0007. _Avoid_: treating it as a database key.

**agent_id (on events)**:
The agent attributed to a webhook event, carried in the [[event envelope]] (a
sibling of `call_id`, not a payload key). For call events it is resolved centrally
in `dispatch_event` from the call's current `active_agent_id` (so a [[handoff]] is
reflected); sms/chat events pass it explicitly. Always present, `null` when no
agent is resolved yet. See ADR-0007.

**ended_reason** (vs **status**):
The granular *why* a call ended (`customer_ended_call`, `assistant_ended_call`,
`voicemail`, `pipeline_error`, `telephony_failed`, …), distinct from `status`
which is the coarse *outcome* (`completed`/`failed`/`no_answer`/`busy`). Derived
— not stored — at [[`call.ended`]] build time from `status` plus the call's
recorded event types (first match in a fixed precedence wins). See ADR-0008.
_Avoid_: kebab-case values; treating it as a stored column.

## Call transfer

**Cold transfer** (= **blind transfer**):
Hand the caller straight to the target number with no operator context —
`<Dial>target</Dial>`; the AI leg drops immediately. The caller may first hear a
[[transfer message]]. The default `transfer_mode`. See ADR-0009.

**Warm transfer**:
Dial the operator first and play them a [[briefing]] (via Twilio [[whisper]])
before bridging the caller, who hears only ringing. `transfer_mode: "warm"`.
Distinct from [[cold transfer]] (no operator context) and from [[handoff]]
(internal agent switch, same call leg, no PSTN dial). See ADR-0009.

**Transfer message**:
A line spoken to the *caller* just before the dial ("Connecting you to
support…"), both modes. The functional successor to the old non-functional
`pre_transfer_message`. Rendered with Twilio `<Say>` in v1. _Avoid_: confusing
with [[briefing]] (that's operator-facing).

**Briefing**:
What the *operator* hears on a [[warm transfer]] before the caller is bridged —
either a literal string or `{from_summary: true}`, which generates a summary from
the transcript via the agent's LLM when the [[whisper]] endpoint is fetched.
_Avoid_: "transfer message" (caller-facing).

**Whisper**:
Twilio's `<Number url="…">` mechanism — after the operator answers, Twilio fetches
the URL and plays its TwiML (the [[briefing]]) to the operator only, then bridges
the caller. The execution substrate for [[warm transfer]]; no conference needed.

**Transfer AMD** (`transfer.answered`):
Answering-machine detection on the operator leg (`machineDetection="Enable"` +
`amdStatusCallback`). It **notifies, it does not block** — the bridge isn't gated
on it, so on voicemail the caller is still connected and can leave a message; the
`transfer.answered` event carries `{target_number, answered_by}` so the backend
knows human vs machine. Preventing voicemail entirely needs the deferred conference
model. See ADR-0009. _Avoid_: assuming AMD aborts the transfer.

## Observability

**Observer** (vs **transcript tap**):
A Pipecat `BaseObserver` attached to the pipeline task (`observers=[...]`) that
*logs* operational signals — latency/TTFB, turn timing, LLM, transcription,
startup. Distinct from the [[transcript tap]] (a `FrameProcessor` in
`observability.py` that writes *product data* — transcripts → DB + webhooks).
Observers attach to the task, so they see **both cascade and S2S**; taps are
cascade-only. The two coexist; observers don't replace taps. See ADR-0010.

**Trace** / **conversation_id**:
An OpenTelemetry trace of one call — a conversation span containing turn spans,
each containing STT/LLM/TTS service spans (TTFB, token counts, fed by the
already-enabled `enable_metrics`). The trace's `conversation_id` **is the
`call_id`**, so a span joins straight back to the call record. Exported to an OTLP
backend; **never console-exported in production** (sync stdout I/O would stall the
audio path — see ADR-0004). See ADR-0010. _Avoid_: console export in prod.

**trace_include_pii**:
The switch (default **on**) that puts `from_number`/`to_number` on spans. Phone
numbers are PII landing in an external tracing backend; flip it off for
compliance-sensitive deployments. The non-PII attributes (`project_id`,
`agent_id`, `direction`, `transport`) are always present. See ADR-0010.

## Access & credentials

**Platform credential** (`PLATFORM_API_KEY` / `X-Platform-Key`):
The single privileged secret gating the bootstrap endpoints — project creation and
first-API-key creation. Identifies the *builder as a caller*, not a user (TurnCall
stays identity-free). Distinct from project-scoped [[API key]]s (`tc_...`), which
authorize everything else. Fails closed: unset means all bootstrap calls are rejected.
_Avoid_: "admin key", "master key" (an admin API key is project-scoped; this is not).

**Frozen credentials**:
The explicit `(access_key_id, secret_access_key, session_token, region)` tuple TurnCall
resolves itself and hands to AWS services, instead of letting boto3 resolve per call.
Forced by [[Nova Sonic]], whose constructor requires explicit credentials while
[[Bedrock]]'s accepts `None` and falls back to boto3's chain — resolving centrally is what
stops one agent config behaving differently by pipeline mode. Re-resolved on each session
rollover so temporary credentials cannot expire mid-call. See ADR-0016.
_Avoid_: "the AWS key" — there is no single key; SSO, IRSA and assume-role all produce a triple.

**Agent AWS credentials** (`AWS_AGENT_CREDENTIALS_ENABLED`):
Per-agent *static* AWS keys — off by default, rejected at agent create when disabled, and
the deliberate escape hatch rather than the normal path (mirroring `BYOM_ENABLED`). The
default multi-tenant route is a per-agent `role_arn` assumed from platform credentials,
which yields temporary credentials and persists no durable secret. The flag exists because
`config_blob` is plain JSONB: secrets are masked on read but not encrypted at rest.
_Avoid_: conflating with [[Platform credential]], which gates bootstrap endpoints and is unrelated.
