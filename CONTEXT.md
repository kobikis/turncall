# TurnCall

Glossary of domain terms specific to TurnCall — the production voice agent platform.
Definitions only; implementation lives in code and ADRs.

## LLM Providers

**Provider**:
The named LLM (or STT/TTS) backend selected per agent — e.g. `openai`, `anthropic`,
`google`, `ollama`, `custom_openai`, `openrouter`, `bedrock`. A provider determines which service
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
_Avoid_: "backup model", "retry model", "model routing"; [[failover]] is the
platform-level mechanism, this is OpenRouter's own.

**Failover**:
Switching one role (STT, LLM or TTS) of a live call to the next [[provider]] in its
ordered list when the active one can no longer do its job, so the call carries on
instead of ending as `pipeline_error`. A recoverable error never triggers it. Not
offered for S2S: the conversation lives inside the realtime session, so there is
nothing to hand over — except an `openai_live` client delegate, which is an
ordinary LLM and fails over like cascade's.
_Avoid_: "fallback" (taken by [[fallback models]]), "backup provider", "retry".

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

**Delegation**:
How the `openai_live` S2S [[provider]] (gpt-live-1) gets work done that needs tools or
reasoning: the live model, which only listens and speaks, hands it to a [[delegate]] and
voices the result in its own words. Two modes, named as OpenAI names them: *client* (the
delegate is the agent's own `llm`) and *responses* (the delegate is an OpenAI-hosted
model). *client* is the default for an agent with tools. No other S2S provider delegates — they call tools themselves. See #241.
_Avoid_: "backend delegation", "tool forwarding".

**Delegate**:
The model gpt-live-1 hands work to under [[delegation]], which runs the agent's tools —
the agent's own `llm` in *client* mode, an OpenAI Responses model in *responses* mode.
_Avoid_: "backend" — taken by [[provider]] and by the builder's Agent Backends, so "the
agent's backend" would mean three things.

**Classifier**:
A small object that answers *typed* questions about some state — yes/no, a
choice among options, or a score — and returns a probability with each answer.
A pipecat type in its own right since 1.12, not a description: an
`LLMClassifier` wraps a [[provider]]'s LLM and asks it for one JSON object,
while a `JevClassifier` calls a purpose-built classification model directly.
Used for [[voicemail detection|Voicemail detector]] and for an eval's
[[judge]]. _Avoid_: calling the LLM *behind* one "the classifier" — the LLM
answers the question, the classifier is what asks it and parses the reply.

## Speech recognition

**Keyterms** (`stt.keyterms`):
Vocabulary hints given to the recognizer so it favours words it would otherwise
mangle — product names, SKUs, surnames. One TurnCall concept; each provider
names it differently (`keyterm`, `keywords`, `keyterms`) and Deepgram picks
between two of its own by model. Say "keyterms" for the platform field and name
the provider's parameter only when discussing that provider's API. _Avoid_:
"keywords" as the general term — on Deepgram it is a specific parameter that is
an error on Nova-3.

## Speech synthesis

**Pronunciations** (`tts.pronunciations`):
A per-agent dictionary from a word to its IPA transcription, so the voice says
"Metformin" and "Kigali" the way they are meant to sound. The outbound mirror of
[[keyterms]]: one TurnCall field, four provider dialects generated from it
(Cartesia inline phonemes, ElevenLabs SSML `<phoneme>`, Deepgram's inline
object, and OpenAI none at all). Validation is TurnCall's own — pipecat's IPA
utilities normalize and tokenize but never reject — and whether a voice can
*read* the markup belongs to the constructed service, not the provider name. On
ElevenLabs that takes `enable_ssml_parsing`, which TurnCall turns on for an agent
that has pronunciations and a phoneme-capable model, and only then: SSML parsing
changes how all of that agent's text is read (#147). The model stays the
author's — a phoneme-incapable one still warns rather than being downgraded, and
has the markup withheld: a `<phoneme>` tag reaching a service that is not parsing
SSML is read aloud, so "spoken as written" means the tag never goes.
Validation is on **notation**, not phonology, so a respelling that happens to
use only IPA-legal characters passes: `met-FOR-min` is a 422 on its hyphen and
capitals, while `metformin` and `met for min` are accepted, stored, and sent to
the voice as real markup (measured on Deepgram, `met for min` synthesizes 25%
more audio than no markup at all — the word is pronounced from those letters).
No validator can catch that class in general, so the config's two advisory
checks — a pronunciation equal to its own spelling, and a voice that will not
read the markup — ride back as **`warnings` on the agent read**, derived on
every read and never stored (#148, the shape #95 set). They are warnings and not
refusals because `{"papa": "papa"}` is correct IPA: refusing correct input breaks
a working agent, where accepting a respelling mispronounces one word.

On ElevenLabs this means the older voice or no pronunciations, and that was
**decided rather than overlooked** (#153, closed `wontfix`): `eleven_v3` reads
IPA natively as `/mɛtˈfɔɹmɪn/` — no tags, no `enable_ssml_parsing` — but only off
pipecat's `ElevenLabsHttpTTSService`, since the TTS `stream-input` WebSocket
refuses both v3 ids with a 400. Measured: the dialect works (2.06x the plain
audio on a long IPA), barge-in still stops immediately there, and it costs ~140ms
more per turn (277ms → 415ms time-to-first-audio, pooled). Declined on that
price against ElevenLabs' stated 80-90% consistency, not on feasibility; worth
revisiting if v3 reaches the WebSocket. What outlives the decision: a stray
`/IPA/` is **read aloud** by `eleven_flash_v2_5` (5.37x the audio), so any future
slash dialect must be withheld by model even more strictly than the tag dialect —
slashes need no flag to be spoken.

_Avoid_: "pronunciation dictionary" for ElevenLabs' hosted, account-level
`pronunciation_dictionary_locators`, which is a different thing entirely (#152
closed `wontfix`: same model gate, and deprecated in pipecat 1.6.0); and
calling a respelling ("met-FOR-min") a pronunciation — only IPA is accepted,
because only IPA can be turned into every provider's markup.

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
  run in series rather than together. On a Deepgram Flux agent it is not read
  at all: Flux decides the turn (`eot_threshold`/`eot_timeout_ms`), and VAD
  stays at 0.2s deciding nothing.
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

**Empty user turn**:
A caller turn that VAD opened and closed with **no transcript** behind it — a
cough, a door, a word the STT could not resolve. A turn *outcome*, deliberately
not a fourth entry in [[the three timeouts]]: nothing here is a duration, and
the confusion this glossary exists to prevent is the one between durations.
Two cases, and they are answered differently because the conversation is in a
different state:
- **Interrupted** — the empty turn cut the agent off mid-reply. Answered: the
  agent would otherwise be silent in the middle of its own sentence, so it asks
  the caller to repeat. On by default.
- **Idle** — the agent had finished and was waiting. Unanswered by default:
  replying to what may have been a passing truck is intrusive, and the
  conversation is not stuck. [[User idle|the three timeouts]] already covers
  the case where it *is*.
_Avoid_: "empty turn" for a turn the caller genuinely stayed silent through —
that is [[user idle|the three timeouts]], and nothing opened a turn at all.

## Ambience

**Ambience**:
The room an agent sounds like it is in — a continuous, quiet bed of sound under
everything it says, playing for the whole call rather than only while it speaks.
It exists so an agent does not sound like it is calling from an anechoic chamber.
Off unless an agent asks for it, and mixed into the *outbound* audio only: it
never reaches the STT, so it cannot be mistaken for the caller.
_Avoid_: "background noise" — that is the thing VAD and STT exist to remove, and
reusing the word here reads as a defect. _Avoid_: "background music" — music is
not what this is for, and it carries a licensing question ambience does not.

**Ambience sound**:
One piece of audio a project uploaded to be used as [[ambience]], owned by the
project and shared by as many agents as reference it. What a caller hears is
never the file that was uploaded: each sound is normalized once, on the way in,
and stored as a [[rendition]] per rate. A sound cannot be deleted while an agent
still names it.
_Avoid_: calling the uploaded file itself the ambience sound — the uploaded bytes
are a source, and nothing plays them.

**Rendition**:
One rate-specific, mono copy of an [[ambience sound]], produced at upload for
each output rate a voice transport runs at. A rendition exists per rate because
the mixer refuses audio at any other rate **silently** — it warns once and then
mixes nothing, so the caller hears no ambience and nothing raises. A missing
rendition is therefore indistinguishable from a working call, which is why they
are made in advance rather than converted on the way out.

**Ambience is in the [[call recording]]**:
On the agent's channel, as the caller heard it. The recorder sits after the
transport's output, and the frames the transport passes on are already mixed.
Transcripts and post-call analysis read text, so they are identical to an agent
without ambience.

## Telephony

**Carrier**:
The telephony company a [[phone number]] belongs to, which connects its calls to the
phone network — Twilio, Telnyx, or a PBX's own Asterisk reached over ARI. Stored as
the number's `provider` column, exposed by the API as `carrier`.
_Avoid_: "provider" for this (a [[provider]] is an STT/LLM/TTS backend), "telephony
provider", "transport" (the media path a call's audio takes — a carrier's media
stream, WebRTC, WhatsApp).

**Campaign**:
A set of outbound calls one agent places to a list of targets, paced and retried by
the platform; the unit an operator starts, pauses and cancels. The agent may be a
weighted split between agents, each target assigned one by its number.
_Avoid_: "batch", "broadcast", "dialer list".

**Concurrency limit** (vs **rate limit**):
A concurrency limit caps how many of something are in progress *at once* (live calls
in a project); a rate limit caps how many *start* per unit of time (outbound calls per
second from a number, messages per minute in a session). Both refuse with
`rate_limited`.
_Avoid_: "quota" (implies a billing allowance), "throttle" unqualified.

**Suppression list**:
A project's numbers that must never be dialled, checked before every outbound call —
a [[campaign]]'s and a single API call's alike. Filled by the operator, or by the
caller asking mid-call (`call.opted_out`). Holds no consent records: consent is the
operator's legal record, not the platform's.
_Avoid_: "DNC list" unqualified (that is the national registry), "blocklist" (reads as
inbound blocking).

**Release** (a number) (vs **unbind**):
Giving a number back to its [[carrier]], so the operator stops paying for it and may
never get it back. Distinct from unbinding, which only stops routing it to an agent and
leaves it on the account. Admin-only, refused while the number is bound, and requires
an explicit confirmation in the request. Its counterpart is buying a number through the
engine on the project's own carrier account. See ADR-0025 (decided, not yet built).
_Avoid_: "delete number" (reads as unbind), "port out" (moving it to another carrier).

## Voicemail

**Voicemail detector**:
The processor that decides whether an outbound call reached a person or an
answering machine, and holds the agent's first words back until it knows. It
asks a [[classifier]] after every transcription but acts on **silence**, not on
an answer: "hi, this is Sam" is both what a person says and how a recorded
greeting starts, and only the pause that follows separates them. A
classification that never arrives is read as *conversation* — treating a person
as a machine is the expensive mistake, so the ambiguity fails toward talking.
_Avoid_: "voicemail LLM" — the LLM answers the question, the [[classifier]]
asks it, and the detector decides when to believe it.

## Call recording

**Call recording** (app-side):
The WAV written to object storage for a call, captured in-pipeline by
`CallRecorder` (a Pipecat `AudioBufferProcessor`) — NOT Twilio's recording API.
Stored at `recordings/{call_id}.wav`; the call row carries [[recording status]]
and `recording_url`. See ADR-0005. _Avoid_: "Twilio recording" (that path exists
in code but is never enabled).

**Recording channels**:
A [[call recording]] is stereo: the caller on the left channel, the agent on the
right. It is never a mono mix, because a mix hides who talked over whom and gives
no clean caller track to reprocess. _Avoid_: "mixed recording".

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
Hand the caller straight to the target number with no operator context; the AI leg
drops immediately. The caller may first hear a [[transfer message]]. The default
`transfer_mode`. See ADR-0009.

**Warm transfer**:
Dial the operator first and play them a [[briefing]] as a [[whisper]] before bridging
the caller, who hears only ringing. `transfer_mode: "warm"`. Behaves the same on every
[[carrier]]: one transfer config never means two things. Distinct from [[cold transfer]]
(no operator context) and from [[handoff]] (internal agent switch, same call leg, no
PSTN dial). See ADR-0009.

**Transfer message**:
A line spoken to the *caller* just before the dial ("Connecting you to support…"),
both modes, in the [[carrier]]'s own voice rather than the agent's. The functional
successor to the old non-functional `pre_transfer_message`.
_Avoid_: confusing with [[briefing]] (that's operator-facing).

**Briefing**:
What the *operator* hears on a [[warm transfer]] before the caller is bridged —
either a literal string or `{from_summary: true}`, which generates a summary from
the transcript via the agent's LLM when the [[whisper]] is played.
_Avoid_: "transfer message" (caller-facing).

**Whisper**:
Audio played to the operator alone, after they answer and before the caller is
bridged — the delivery of a [[briefing]]. The idea, not a mechanism: each [[carrier]]
provides it its own way (Twilio's `<Number url>`, a separate operator leg on Telnyx).
No conference needed.
_Avoid_: using it to name one carrier's mechanism.

**Transfer AMD** (`transfer.answered`):
Answering-machine detection on the operator leg. It **notifies, it does not block** —
the bridge isn't gated on it, so on voicemail the caller is still connected and can
leave a message; the `transfer.answered` event carries `{target_number, answered_by}`
so the backend knows human vs machine. The same on every [[carrier]], including those
that could block: preventing voicemail is the deferred conference model's job, for all
carriers at once. See ADR-0009. _Avoid_: assuming AMD aborts the transfer.

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

**Turn metrics**:
The timing record of one conversational turn: time to first byte for each STT,
LLM and TTS stage, plus user-to-bot latency. It is kept as part of the call
record, unlike an [[observer]]'s output, which is only logged. A call's
**latency summary** is derived from its turn metrics and never stored.
_Avoid_: "latency logs".

**trace_include_pii**:
The switch (default **on**) that puts `from_number`/`to_number` on spans. Phone
numbers are PII landing in an external tracing backend; flip it off for
compliance-sensitive deployments. The non-PII attributes (`project_id`,
`agent_id`, `direction`, `transport`) are always present. See ADR-0010.
ADR-0024 flips the default to **off** from the public release.

**Caller-ear latency** (vs **pipeline latency**):
The wait a caller actually hears: from the end of their speech to the agent's first
audio, measured in the call's stereo [[call recording]] over a real carrier. Includes
endpointing and the carrier leg, which [[turn metrics]] and the latency summary do
not. The number TurnCall publishes. See ADR-0023.
_Avoid_: "latency" unqualified when comparing with other platforms; "TTFB" (one
stage's time to first byte).

**Usage**:
What one call consumed, per [[provider]] and role: STT seconds, LLM input and output
tokens, TTS characters, carrier minutes. Recorded on the call. See ADR-0027 (decided,
not yet built).
_Avoid_: "billing" (TurnCall bills nobody), "metering" unqualified.

**Cost estimate** / **price table**:
A call's [[usage]] priced from the price table: list prices shipped with the engine
for every provider and model, overridable per project with negotiated rates. Always
an estimate, never an invoice; the provider's bill is the truth. See ADR-0027.
_Avoid_: "cost" unqualified in UI copy, "spend".

## Privacy & retention

**Redaction**:
Replacing PII (entity types the project chooses) in everything that outlives the live
call or leaves the process: the stored transcript, analysis, takeaways, webhook
payloads, logs and spans. **Never** what the model hears during the call, which still
needs the caller's number to book the appointment, nor what the call itself reads on
its behalf afterwards (a warm transfer's briefing, post-call analysis and takeaways read
the conversation as said; their *outputs* are redacted on the way out). Text only; done
locally (Presidio), opt-in per project. A redacted value reads `<ENTITY_TYPE>`. Built for
the transcript and call events at the event exit point (#313); analysis as stored,
tool-invocation rows, text messages, logs and spans are #314. See ADR-0024.
_Avoid_: "masking" (reads as display-only), "anonymization" (a stronger legal claim).

**Retention policy**:
How long a project keeps recordings, transcripts and events, set separately for each,
overridable per agent, enforced by a janitor. Unset means keep everything, the
behaviour before it existed. Never applies to the [[audit log]]. See ADR-0024.
_Avoid_: "TTL", "auto-delete", confusing it with the soft-delete purge of whole
projects (`PROJECT_PURGE_RETENTION_DAYS`, ADR-0011).

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
`config_blob` is plain JSONB: secrets are masked on read but not encrypted at rest. It
governs the *inline* path only — a [[Project credential]] is encrypted and outside agent
config, so this flag neither gates nor describes one (ADR-0020). Read as "no static AWS
keys here" it is a security flag misread in the unsafe direction.
_Avoid_: conflating with [[Platform credential]], which gates bootstrap endpoints and is unrelated.

**Number's Twilio account**:
The Twilio account a [[phone number]] runs on when it was bound with its own Account SID and
Auth Token, instead of the platform's `TWILIO_*`. Kept per *number*, not per project, because
Twilio's unit is the number: every webhook names its account by `AccountSid`, and one project
can hold numbers from two accounts. The SID is stored in the clear (webhooks are matched by
it), the token sealed like a [[Project credential]]. See `services/twilio_accounts.py`.
_Avoid_: "the project's Twilio", "Twilio credential" unqualified.

**Project credential**:
A provider account's key held per project in its own encrypted table, rather than in agent
config — one row per provider *account*, never per role, since Deepgram is one key serving
STT and TTS and OpenAI one key serving four. Resolved below an agent's own inline key and
above the platform environment ("narrowest wins", the ordering the eval [[judge]] already
uses) — at pipeline build for a call, and the same way for a text turn, a post-call analysis,
a knowledge-base embedding and an eval run. Exists because the alternative — an `api_key` on every config
block — writes third-party secrets into `config_blob`, the unencrypted JSONB that
[[Agent AWS credentials]] is switched off by default to stay out of. See ADR-0020.
_Avoid_: "integration" (the Builder's word for the console-side record it is projected
from), "the project's API key" (that is a `tc_...` [[API key]], authorizing calls *to*
TurnCall rather than out of it).

**Audit log**:
An append-only record of changes made through the API (agents, credentials, numbers,
API keys, project settings), each entry keyed by the [[API key]] that made it, since
TurnCall is identity-free. Kept under its own retention, never the call
[[retention policy]]. The builder audits the person behind a console change and links
to these entries. See ADR-0024 (decided, not yet built).
_Avoid_: "activity feed" (reads as user-facing history), "event log" (that is a
call's events).

**Session token** (browser):
What a browser presents to join a WebRTC call instead of an [[API key]]: minted by the
customer's server, valid for one call, five minutes and one agent, with the template
variables sealed by the minting server so a visitor cannot change them. There are no
publishable keys. See ADR-0026 (decided, not yet built).
_Avoid_: "public key", "client key" (implies a long-lived credential), "media token"
(the carrier stream's proof, `services/media_token.py`).

## Evals

**Scenario**:
One saved behavioural test, of exactly one [[kind]]. Its `definition` is pipecat's own
scenario mapping, stored verbatim and validated by round-tripping it through pipecat's
parser — the schema belongs to pipecat and moves between majors, so `schema_version`
records which one it targets. `tool_mocks` and `tool_policy` sit beside it as TurnCall
columns, deliberately outside the mapping we do not own — `tool_mocks` is a map of
tool name to the canned result the model is handed instead of the call being dispatched,
and `tool_policy` (`mock_only` by default, `live` the typed opt-in) decides what happens
to a tool no mock covers: refused, and the iteration errored naming it. See ADR-0018.
_Avoid_: "test suite" — the name of the dead stub this replaced (#69), and of the
abstraction Vapi retired in favour of Simulations. Grouping is `tags` and [[batch]].

**Derived scenario** (a **draft**):
A [[scenario]] converted from a conversation that already happened, rather than written:
`from-call` for a voice call, `from-session` for an SMS, chat or WhatsApp one. Caller
utterances become turns, agent replies become expectations, and each recorded tool
invocation becomes both a `function_call` expectation carrying the arguments actually
used and a [[tool mock]] seeded with what that tool really returned — so the draft runs
safely under `mock_only` without repeating the call's side effects. It is a **draft** and
the response says so: unedited, it asserts whatever the agent did that day, mistakes
included. Evals only find what someone thought to test; the unknown unknowns come from
production, which is why this loop exists at all.
_Avoid_: "recorded test", or any wording that implies the expectations were reviewed.

**Run events**:
`eval.run.started` and `eval.run.completed`, the only two an eval dispatches. The
completed one is comprehensive — the whole result, including the three snapshots
— following `call.ended`'s precedent (ADR-0006) rather than
[[analysis.completed]]'s, which is reserved and never dispatched. The run's
identity rides in the **envelope** as `eval_run_id`, beside `call_id` and
`session_id` (ADR-0007), additive and nullable so no existing subscriber breaks.
An errored run dispatches one too: `errored` is terminal.
_Avoid_: putting the run id in the payload, or splitting the result across
per-iteration events.

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
What decides a verdict. Distinct from the [[persona]]; both are models TurnCall runs, and
neither is the agent. Pipecat's `EvalJudge`, which defaults to a **local Ollama** — so a
deployment on [[Bedrock]] for data residency sends transcripts nowhere by default, and the
real cost is the mirror image: an `eval:` assertion errors until an Ollama is reachable.
Assertions that only use `text_contains` or `function_call` build no judge at all.

Since pipecat 1.12 the verdict itself is a [[classifier]]'s, not an LLM's prose answer —
the judge picks among outcomes and carries a confidence. Because a classifier gives no
reasons, a second model, the **explainer**, is asked for the reason behind every `no` and
every verdict it is unsure of. The explainer never changes a verdict; where the two
disagree, the reason says so.

Which model explains is TurnCall's choice, not pipecat's (#139). For an LLM-backed judge
pipecat explains with the judging model itself, which is what anyone would expect and is
left alone. For a judge that is **only** a classifier — `jev` — there is no such model,
and pipecat's fallback is its own default LLM service, a local Ollama: a judge configured
against a hosted vendor would silently acquire a dependency on a service nobody
provisioned, and say so as an unreachable connection mid-run. So the explainer is compiled
from the platform's configured judge, or reasons are turned **off** explicitly where the
platform has configured none. `harness_config` records `explainer_used` /
`explainer_provider` / `explainer_model`, and the `judge_changed` warning covers them.
_Avoid_: reading a verdict with no prose as a broken run — probabilities and no reason is
the deliberate outcome on an unconfigured platform.

That confidence is recorded beside every verdict in a [[run]]'s `results` (#138), so a
marginal call and a clear-cut failure stop reading the same — nothing aggregates it and
nothing fails on it. `null` is "the judge reported none", which a `text_contains` or a
`function_call` always is; `0.0` is a judge certain of the opposite. Pipecat 1.12 drops
the value building its result objects, so runs read `null` until it stops.
_Avoid_: averaging it into a run-level number, or reading `null` as an unsure verdict.

A scenario names its own with `judge: {provider, model, temperature, endpoint}` and
`simulator: {...}` — TurnCall columns beside `tool_mocks`, compiled into pipecat's blocks
at parse time. `provider` is a closed set (`ollama`, `openai`, `anthropic`, `jev`) mapping to
factories **TurnCall ships**, because pipecat's own escape hatch is `factory`: a dotted
path it hands to `importlib.import_module`, which from a request body is remote code
execution. The key is the platform's, never the scenario's.

`jev` (#141) is TypeSafe's [[classifier]]: a few hundred milliseconds per verdict against
several seconds for a local LLM, and a *calibrated* probability rather than a model's own
guess at its confidence. **Judge-only, and structurally so** — the allowlist is split by
role (`PROVIDERS` is what a [[persona]] may be, since a persona is linked into a pipeline
to speak; `JUDGE_PROVIDERS` is the superset), so a `simulator:` naming it fails the
existing unknown-provider check with the role in the message rather than needing a rule of
its own. It takes no temperature — a classifier is not a sampler. `TYPESAFE_API_KEY` and
the `evals-jev` extra are both properties of a *deployment*, so a scenario naming `jev`
stores and reads back on a platform with neither; the **run** is what fails, naming which
of the two is missing. Not a candidate for [[voicemail detection|Voicemail detector]]:
that is the live call path, where a new vendor gets its own measurement (ADR-0019). `EVAL_JUDGE_PROVIDER` /
`_MODEL` / `_TEMPERATURE` set one for the whole platform; precedence is narrowest-wins
and a block is taken whole. A **run** cannot override it — that would make two runs of one
scenario incomparable with nothing on either row saying why.

The judge is the weakest component: verdicts are cached within a run but not across runs,
so a scenario can flip with no code change, and a silent provider-side model update moves
the whole baseline. `harness_config` records provider, model, temperature, service,
factory, the explainer's provider and model, and whether a judge was asked anything at all — and a run whose judge differs
from this scenario's last verdict carries a `judge_changed` warning, because otherwise a
red run reads identically whether the agent regressed or the judge moved. See ADR-0018.
_Avoid_: reading a recorded judge model as proof one ran — `judge_used: false` means
every expectation was checked directly.

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
and a result is uninterpretable without them. ADR-0017's rule one level out. The third
also names the **worker** that executed it (`worker_version`, `worker_started_at`,
`worker_stale`): a process older than the code it runs produces a result that looks
ordinary and silently lacks whatever shipped since, which cost a day to diagnose once.

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
