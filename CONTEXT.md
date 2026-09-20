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

## Speech recognition

**Keyterms** (`stt.keyterms`):
Vocabulary hints given to the recognizer so it favours words it would otherwise
mangle — product names, SKUs, surnames. One TurnCall concept; each provider
names it differently (`keyterm`, `keywords`, `keyterms`) and Deepgram picks
between two of its own by model. Say "keyterms" for the platform field and name
the provider's parameter only when discussing that provider's API. _Avoid_:
"keywords" as the general term — on Deepgram it is a specific parameter that is
an error on Nova-3.

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

**The three timeouts**:
Three unrelated durations, all of which someone will call "the silence timeout".
Name which one is meant.
- **Turn silence** (`silence_timeout_ms`, 800ms): how much quiet ends the
  caller's sentence. Tuning this changes how eagerly the agent replies. It is
  the VAD stop window only when Smart Turn is off; with Smart Turn on the
  model decides the turn and VAD drops to its own 0.2s, because the two waits
  run in series rather than together.
- **User idle** (`user_idle_timeout_ms`, 10s): how long the caller may stay
  quiet *after the agent has finished speaking* before the idle guard reacts —
  first the [[idle nudge]], then [[customer_silent]]. `0` disables it.
- **Pipeline idle** (300s, Pipecat's own): how long the whole pipeline may see
  no activity before the runner gives up. Not configurable per agent, and
  reached only when something is already wrong.
_Avoid_: "silence timeout" unqualified, and "idle timeout" for the first of the
three.

**Idle nudge**:
What the agent says on the first [[user idle|the three timeouts]] expiry —
"Are you still there?". Cascade speaks `idle_message` verbatim through TTS; S2S
has no TTS stage, so the model is asked to check in and phrases it itself. One
nudge only: the second consecutive silence ends the call.

**customer_silent**:
The [[ended_reason]] for a call the idle guard gave up on — the caller stopped
responding and never came back. Distinct from `customer_did_not_answer`, which
is a call that was never picked up, and from `customer_ended_call`, which is a
caller who hung up. The strikes count *consecutive* silences: a caller who
pauses, answers, then pauses again starts over.

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
agent is resolved yet — and `null` for the whole call when it runs an
[[inline agent]], which is not a gap to be filled. See ADR-0007.

**ended_reason** (vs **status**):
The granular *why* a call ended (`customer_ended_call`, `assistant_ended_call`,
`voicemail`, `pipeline_error`, `telephony_failed`, …), distinct from `status`
which is the coarse *outcome* (`completed`/`failed`/`no_answer`/`busy`). Derived
— not stored — at [[`call.ended`]] build time from `status` plus the call's
recorded event types (first match in a fixed precedence wins). See ADR-0008.
_Avoid_: kebab-case values; treating it as a stored column.

## Agent resolution

**Inline agent** (vs **stored agent**):
An agent a call runs that exists only for that call — its whole configuration
arrives in the [[call-init]] response instead of an id, so there is no row in
`agents` and nothing that survives the call. The point of it is credentials that
*must not* be stored: a header minted for one call and expiring within the hour.
A [[stored agent]] is the ordinary case, addressed by id and reusable.
_Avoid_: "dynamic agent" (that is the sentinel's name, not the concept),
"anonymous agent", "temporary agent"

**No agent row**:
What an [[inline agent]] means everywhere downstream: `active_agent_id` is null,
[[agent_id (on events)]] is null, per-agent listings do not contain the call, and
knowledge-base attachments resolve to none. Not an error state and not
incomplete data — the honest answer to "which stored agent was this", which is
"none of them". The configuration the call actually ran with is recorded on the
call itself, so post-call work has something to read. See ADR-0017.
_Avoid_: treating it as an unresolved agent, or filling the null with a sentinel

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

## Evals

**Scenario**:
One saved behavioural test, of exactly one [[kind]]. Its `definition` is pipecat's own
scenario mapping, stored verbatim and validated by round-tripping it through pipecat's
parser — the schema belongs to pipecat and moves between majors, so `schema_version`
records which one it targets. `tool_mocks` and `tool_policy` sit beside it as TurnCall
columns, deliberately outside the mapping we do not own. See ADR-0018.
_Avoid_: "test suite" — the name of the dead stub this replaced (#69), and of the
abstraction Vapi retired in favour of Simulations. Grouping is `tags` and [[batch]].

**Scripted** (kind):
A fixed conversation with per-turn expectations. Answers *"at this point, did the agent
make the right next decision?"* Declared by `turns:` in the definition; stored as the
`kind` column, computed eagerly at the API boundary so readers never sniff nullability.

**Simulation** (kind):
A [[persona]], a goal and success criteria; an LLM plays the caller and improvises.
Answers *"by the end, did it reach the right outcome?"* Declared by `persona:`. Exactly
one of `turns:`/`persona:` is present; both, or neither, is rejected at create.

**Persona**:
The simulated *caller's* character and behaviour. Never the agent under test.

**Judge**:
The LLM that decides a verdict. Distinct from the [[persona]]; both are LLMs TurnCall
runs, and neither is the agent. Pipecat's `EvalJudge`, so OpenAI-family only — a
deployment on [[Bedrock]] for data residency points it at a self-hosted
OpenAI-compatible endpoint rather than sending transcripts to OpenAI. The judge is the
weakest component: verdicts are cached within a run but not across runs, so a scenario
can flip with no code change, and a silent provider-side model update moves the whole
baseline. Recorded in the run's `harness_config` for that reason.

**Modality**:
`text` (no STT, no TTS) or `audio` (real speech both ways). One knob on the [[run]] that
sets pipecat's independent `user.modality` and `judge.modality`; a scenario naming either
one keeps its own, which is how the asymmetric pair (`user: audio, judge: text` —
exercises STT, skips TTS) stays reachable. Text mode inverts the coverage: it is the mode
people actually run per-PR because it is fast and free, and it is blind to STT, TTS,
VAD/turn timing, interruption, pronunciation, and the agent's `first_message`.

**Iteration**:
One execution of a scenario. A [[simulation]] needs several — one proves nothing.

**Run**:
One scenario × target × [[modality]], over N [[iteration]]s. **Batch**: the runs produced
by one request. A run carries three snapshots — the agent config that actually ran, the
scenario as it stood, and the [[judge]]/pipecat versions — because all three can change
and a result is uninterpretable without them. ADR-0017's rule one level out.

**errored** (vs **failed**):
`failed` is the agent falling short. `errored` is the *harness* not completing — a connect
failure, a judge outage, a worker that crashed and left its run claimed. An errored run is
neither a pass nor a fail and never counts toward a rate: a judge outage reading as an
agent regression is how a suite loses its audience. There is no score column; a scripted
scenario yields pass/fail and a simulation a rate, and `passed_count`/`failed_count` out
of `iterations` represents both honestly (`1/1`, `7/10`).
_Avoid_: treating `errored` as a kind of `failed`, or rendering it as a red cross.

**Eval worker** (`turncall-eval-worker`):
The separate process that executes runs — never the API process. ADR-0004: an eval does
full STT+LLM+TTS at maximum speed, several at once, and that event-loop jitter would
become dead air for someone on a live call. Same image, new entrypoint, own concurrency
cap, fed by a Redis list. See ADR-0018.
