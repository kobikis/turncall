# ADR-0022: The carrier boundary

**Status:** Accepted
**Date:** 2026-10-02
**Relates to:** ADR-0004 (call audio QoS), ADR-0009 (warm/cold transfer),
ADR-0020 (project credentials).

## Context

Twilio has been the only [[carrier]], and `TelephonyAdapter` grew into its
shape: next to the REST actions every carrier has (hang up, transfer, send
DTMF, configure a number) it carries `generate_media_stream_twiml`, which only
Twilio can answer. Inbound handling lives under `/webhooks/twilio/*`, and the
media entry point `/ws/media-stream` builds a Twilio serializer unconditionally.

Telnyx comes next and Asterisk ARI after it. The three disagree on everything
at the edge: how a webhook is signed (Twilio's HMAC, Telnyx's ed25519 public
key, ARI's authenticated WebSocket), what a webhook looks like, and how a call
is answered (TwiML, a Call Control REST command, an ARI external-media
channel). ARI is not webhook-shaped at all. Past the edge they agree: a
bidirectional media stream into one pipeline.

## Decision

**`TelephonyAdapter` is REST actions only.** Answering a call leaves it;
`generate_media_stream_twiml` moves into the Twilio webhook module.

**Each carrier owns its inbound edge**: its own routes (`/webhooks/twilio/*`,
`/webhooks/telnyx/*`, an ARI client for Asterisk), its own signature check, its
own answer format. Each resolves the number to an agent and hands over to **one
shared media entry point**, which picks the serializer by carrier. Everything
after the serializer — pipeline, recording, ambience, voicemail detection,
tools, analysis — never learns which carrier it is on.

**Telnyx uses Call Control, not TeXML.** TeXML is a TwiML clone and would have
reused the most code, which is the objection: it shapes the second carrier
after the first. Call Control is Telnyx's own model and what pipecat's
`TelnyxFrameSerializer` is written against.

**Pipecat's serializers for new carriers.** ADR-0004 is why Twilio has its own
(stateless resampling clicked at every frame). Pipecat's Telnyx serializer
already keeps resampler state, so the reason does not carry over.

**One behaviour per transfer config.** A warm transfer on Telnyx keeps
ADR-0009's semantics exactly — transfer AMD notifies and does not block, a
no-answer plays `fallback_message` and ends — even though Call Control could
do more. An agent's transfer config must not mean two things depending on
which carrier a number happens to sit on. The improvements belong to the
deferred conference model, for every carrier at once.

**Credentials follow ADR-0020**: a `telnyx` project-credential account over a
platform `TELNYX_API_KEY`. The per-number account Twilio has stays a Twilio
feature until someone needs it elsewhere.

**Naming.** The column stays `phone_numbers.provider` (a rename is a migration
for no behaviour); the API exposes it as `carrier`, with `provider` accepted
as a deprecated alias, because "provider" means STT/LLM/TTS everywhere else.

## Considered options

- **One route set, `answer_call()` on the adapter.** Rejected: it only works
  while every carrier is webhook-shaped, which ends at the third.
- **TeXML for Telnyx.** Rejected above.
- **Carrier-specific transfer behaviour.** Rejected above.

## Consequences

- Adding a carrier is an adapter, an edge module and a serializer choice; the
  pipeline does not change.
- Telnyx's audio is 8 kHz, a rate already in `VOICE_OUTPUT_RATES`, so ambience
  renditions and recording need nothing new.
- The [[whisper]] and [[transfer message]] are defined by what the operator
  and caller hear, not by `<Number url>` and `<Say>`; each carrier provides them
  its own way.
