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

## Addendum (2026-10-09): Asterisk ARI (#209)

The case this ADR was cut for. What the ticket left to refine, decided:

- **The edge is a classic ARI client.** TurnCall connects out to
  `/ari/events?app=…` on each Asterisk it serves and keeps the connection
  open; REST actions are ARI HTTP requests. Not ARI *outbound* WebSockets
  (Asterisk dialling TurnCall per call): those would spread calls over
  processes, but hang-up and transfer from the API still need ARI's HTTP side,
  and a fake ARI server is the same shape as the real one. The price is one
  connection per app: ARI gives an app's events to the newest WebSocket only,
  and the replaced one is not handed them back (measured on Asterisk 23.4.1),
  so exactly one replica runs the edge (`ASTERISK_EDGE_ENABLED`). An HA edge is
  deferred; outbound WebSockets are the likely way there.
- **Media is chan_websocket external media**, not RTP or AudioSocket. The
  WebSocket lands in the one shared media entry point like Twilio's stream,
  with `FastAPIWebsocketTransport`; RTP and AudioSocket would each need a
  UDP/TCP listener beside it and their own NAT story, and pipecat 1.12 has a
  serializer for none of the three. It needs Asterisk 20.18 / 22.8 / 23.2:
  JSON control messages (`transport_data=f(json)`), and a `MEDIA_START` that
  names the channel id — the call id travels in it (`turncall-media-<id>`),
  since `v()` URI parameters leaked between calls (asterisk#2194). (Since
  the review of #212, the call's media token travels there too:
  `turncall-media-<id>-<token>`; see the addendum below.) ulaw at
  8 kHz, a rate `VOICE_OUTPUT_RATES` already renders ambience at.
- **Two channels, one call.** The caller's channel and the media channel are
  bridged, and ARI ties neither's end to the other's: the edge hangs up the
  caller when the media ends, and the media when the caller leaves the app —
  which a transfer also does, so a transferred caller is never hung up.
- **A call the edge cannot take goes back to the dialplan**, unanswered, at
  the priority after `Stasis()`: a PBX has its own fallback (a queue,
  voicemail), which a refusal spoken by TurnCall would pre-empt.
- **Transfer is cold only**: `continue` in a transfer context with the target
  as extension, so the PBX's dialplan dials it. A warm transfer, a
  `transfer_message` and a `fallback_message` are refused with a reason the
  model reads (`UnsupportedCarrierAction`) — this ADR's one-behaviour rule
  applied as a refusal, since ARI has no single request that keeps ADR-0009's
  semantics. Transfer AMD only ever notified, so it is skipped. Hang-up and
  DTMF are the obvious ARI requests on the caller's channel.
- **No outbound, SMS or number configuration** on Asterisk numbers: the
  dialplan routes the number, and outbound from a PBX is its own ticket.
- **Credentials**: an `asterisk` project credential (URL, login, app, media
  connection, transfer context) over the platform's `ASTERISK_*`, as ADR-0020.
  A project rides one Asterisk — its own, else the platform's — so a call's
  REST actions reach the PBX that rang it; the platform edge refuses numbers
  of a project with its own. The URL passes the BYOM SSRF gate, since TurnCall
  connects out to it.

## Addendum: a media stream proves it is the call's

The shared media entry point first trusted the call id each edge handed it.
A call id is not a secret, so any socket naming one — on any carrier's route,
any number of times — got the call's real pipeline. Each call's stream setup
now also hands its carrier a **media token**, an HMAC of the call id keyed by
`API_KEY_HASH_SECRET` (stateless, so any replica checks a token another
minted), and the entry point refuses before building anything: a stream
without the token, one on a route whose carrier is not the call's, and a
second stream for one call (`calls.media_attached_at`, a guarded UPDATE).
Where the token rides is each carrier's own: Twilio's TwiML `<Parameter>`,
Telnyx's `stream_url` path, Asterisk's external-media channel id.
