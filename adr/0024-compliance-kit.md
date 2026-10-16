# ADR-0024: The compliance kit: redaction, retention and an audit log in the engine

**Status:** Accepted (redaction of the transcript and call events built in
#313; the rest of redaction in #314; retention and the audit log not yet)
**Date:** 2026-10-16
**Relates to:** ADR-0005 (call recording), ADR-0010 (OTel tracing), ADR-0011
(project deletion), ADR-0013 (takeaways), ADR-0015 (open-core licensing).
Tracked in #306.

## Context

Self-hosting keeps calls in the operator's own cloud, and that is where a
security review starts, not where it ends. The next questions are what PII is
stored and sent, how long it is kept, and who changed what. TurnCall has no
answer to any of them: no redaction, no retention beyond purging soft-deleted
projects (`PROJECT_PURGE_RETENTION_DAYS`), no audit log, and
[[trace_include_pii]] on by default.

The managed platforms sell these as add-ons (Vapi's HIPAA package is
$2,000/month; Retell charges per minute for PII removal; ElevenLabs keeps BAAs
for Enterprise). No open-source platform has them in its code; Dograh keeps
audit logs for its paid tier. Regulated self-hosters are the second buyer we
build for, after developers embedding voice.

## Decision

**All of it ships in the MIT engine. Nothing is a paid tier.** The pitch is that
a self-hosted TurnCall is cheaper to certify than a managed platform's add-on;
holding the controls back would undo it. (ADR-0015's line still holds: the
builder is the commercial product, the engine is for adoption.)

**Redact what leaves the call, never what the model hears.** An agent that
cannot see the caller's phone number cannot book the appointment. Redaction
applies to everything that outlives the live call or leaves the process: the
stored transcript, analysis, takeaways, webhook payloads, logs and spans.

- **Text only in v1.** For audio, the guidance is to turn recording off or give
  recordings a short retention. Bleeping the recording comes later.
- **A local detector:** Presidio with spaCy's small English model, so no text
  goes to a vendor to be redacted. It is in the default image and loaded only
  when a project turns redaction on. Other languages come later.
- **Opt-in per project,** with configurable entity types.

**Retention is a policy the operator sets; the default keeps everything.** Per
project, overridable per agent, set separately for recordings, transcripts and
events. A janitor enforces it, and `call.ended` states what will be kept. A
default that deletes would surprise every upgrade.

**The audit log is append-only and keyed by API key.** The engine has no notion
of a person (it is identity-free, and the [[platform credential]] identifies the
builder, not a user), so an entry records which API key changed an agent, a
credential, a number, a key or a project setting. Call retention never touches
it; it has its own retention setting. Changes made by a person in the console
are audited by the builder, which links its entries to these (builder-api
ADR-0028).

**[[trace_include_pii]] defaults to off from the public release.** Caller
numbers in an external tracing backend should be a choice. This is a behaviour
change and goes in the changelog.

**A mapping document, not a certification.** One page that maps each control to
GDPR first, then HIPAA, and says plainly that TurnCall itself holds no
certification.

## Considered options

- **Redaction by the STT provider** (Deepgram and AssemblyAI can redact). It
  redacts what the model hears, which breaks the agent, and only covers
  providers that offer it.
- **An LLM-based redactor.** Better recall on free text, but it sends the text to
  a model provider to redact it, which defeats the purpose.
- **Audit in the builder only.** Misses every change made straight against the
  API, which is how developers use the engine.

## Consequences

- The image gains Presidio and a small spaCy model: ~74 MB of the pull, but
  ~255 MB unpacked (spaCy's compiled code, phonenumbers' geodata, blis, thinc,
  the model), about 7% of the image. *Corrected 2026-10-16 (#313): this said
  "tens of MB, not hundreds", which holds only for the compressed pull.* A
  process holding the loaded detector uses ~200 MB more memory, and only a
  process that has redacted something holds it.
- Redaction runs before storage and before webhook dispatch, so a webhook
  consumer never sees what was redacted; that has to be stated in the webhook
  docs.
