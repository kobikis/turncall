# ADR-0024: The compliance kit: redaction, retention and an audit log in the engine

**Status:** Accepted, fully built (redaction of the transcript and call
events in #313; the rest of redaction in #314; `trace_include_pii` off by
default in #315; retention in #316; the audit log in #317; the mapping
document in #318)
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
- **The detector runs in a worker process, not on the event loop and not on a
  thread** (#313). Every live call's audio in a process shares one loop
  (ADR-0004), and Presidio is mostly Python, so it holds the GIL even on a
  thread of its own. Measured on arm64 (8 cores), with N simulated calls each
  ticking every 20 ms with ~0.3 ms of CPU per tick, utterances redacted every
  1.5 s per call and a ~7.6k-character transcript every 2 s, the loop's tick
  lateness was:

  | | p99, 20 calls | max, 20 calls | p99, 40 calls | max, 40 calls |
  |---|---|---|---|---|
  | No redaction (2 runs) | 7.0-12.4 ms | 10-37 ms | 10.5 ms | 12 ms |
  | One-thread executor (6 runs at 20) | 16-46 ms | 24-91 ms | 24-25 ms | 32-39 ms |
  | One worker process (6 runs at 20) | 6.2-10.1 ms | 9-69 ms | 10.4 ms | 17-27 ms |
  | Two worker processes (2 runs) | 6.5-6.8 ms | 13-15 ms | 10.8-11.6 ms | 26-28 ms |

  One process run, the first, had a p99 of 113 ms (max 190 ms); five more
  did not reproduce it. With a transcript every 0.5 s (the worker saturated),
  p99 was 7.0 ms without redaction, 21 ms on the thread and 6.5 ms in one
  process. These are laptop runs and noisy; the process numbers track the
  baseline, the thread's are two to four times it. One worker, spawned (not
  forked, as the server runs a loop and threads), loaded once (~200 MB). The app's own process never imports
  Presidio. Work in flight is bounded and slow redactions are logged; a
  detector that cannot load or keep up withholds text rather than send it raw.
- **No detector wait inside a database transaction** (#313). Call events are
  stored in their caller's transaction, under a per-call lock; a wait for the
  detector there would hold the lock and a pooled connection for as long as
  the redaction queue. Text is redacted before the transaction; the
  transaction-scoped store only takes payloads already redacted, or ones with
  no text.
- **Opt-in per project,** with configurable entity types.

**Retention is a policy the operator sets; the default keeps everything.** Per
project, overridable per agent, set separately for recordings, transcripts and
events. A janitor enforces it, and `call.ended` states what will be kept. A
default that deletes would surprise every upgrade.

- **Retention as built** (#316). Whole days from the call's end, 1 to 36500,
  null = kept for good; `PUT /v1/projects/{id}/retention` (admin key) and an
  additive `retention` block in the agent config, field by field over the
  project's. The agent's is read from its live version, so it covers the
  calls of all its versions, as a project change covers its stored calls: a
  policy applies retroactively, both ways. The **transcript** is the
  conversation and what was made from it: the transcript and tool events,
  injected messages, tool-invocation rows, and the post-call analysis with
  its takeaways, which restate what was said. **Events** are the rest of the
  call's log. The call row is never deleted; `*_expired_at` columns say what
  went, and `ended_reason`, derived from the events, is frozen on it before
  they go. Text other events quote from the conversation (the fields
  redaction analyses: a handoff's reason and context, a transfer's words)
  is cleared with the transcript; the events stay with the events kind,
  since `ended_reason` depends on them. An agent's `retention` overrides an
  admin-only setting, so changing what applies to an agent's calls needs an
  admin key too. A recording goes from storage, from the Twilio account
  when its `recording.ready` names a carrier copy, and from the event log's
  URLs; `recording_status` becomes `expired`, and `recording.ready` stays
  while the recording does. The carrier's copy is deleted only where
  TurnCall heard of it and holds the account's credentials: otherwise your
  carrier may keep its own copy; delete it there or turn carrier recording
  storage off. Objects are deleted before the row is marked, outside any
  transaction (collect, delete, then mark only rows unchanged since), so a
  failed delete is retried rather than orphaned and no row is locked while
  storage answers. Transcripts and events are claimed with `FOR UPDATE SKIP
  LOCKED`; the janitor runs hourly on every API replica and writes only
  `calls`, `call_events` and `tool_invocations` (a test holds that, so the
  audit log stays out of its reach). An analysis write is conditional on the
  transcript still being there, so a re-run racing the janitor stores
  nothing. Calls only: text conversations are not covered yet.

**The audit log is append-only and keyed by API key.** The engine has no notion
of a person (it is identity-free, and the [[platform credential]] identifies the
builder, not a user), so an entry records which API key changed an agent, a
credential, a number, a key or a project setting. Call retention never touches
it; it has its own retention setting. Changes made by a person in the console
are audited by the builder, which links its entries to these (builder-api
ADR-0028).

- **Audit log as built** (#317). Every mutating route on agents (their
  versions, publish, rollback and knowledge base links), project
  credentials, phone numbers, API keys and project settings (limits,
  redaction, retention, deletion) takes one audit trail
  (`api/audit.py`); a test lists every such route and fails on one without
  it. The trail is bound to the request's session: its entries are written
  in one INSERT as the session commits, and a commit with no entry is
  refused, rolling the change back, whichever code commits (a route that
  must commit before a side effect is held to it too). So an entry is
  written in the change's transaction: a change rolled back has none, and a
  saved one always has one. A request writes one entry per resource it
  changed (a publish writes the version, each version it archived and each
  number it re-pointed), all sharing a `request_id`: the id of its first
  entry, the one for its primary resource, which is the single id its
  response names in `X-TurnCall-Audit-Id`. One id rather than all of them
  keeps the header bounded however many numbers a publish re-points; the
  builder links its entry to that one and reads the rest by
  `?request_id=`. The platform credential's bootstrap of a project and its first
  key is recorded too, as `actor: platform` with no key. **What changed** is
  a diff of the resource as the API reads it, field by field (each named by
  its RFC 6901 JSON Pointer, so a key holding a `.` never collides with a
  nested one), its "before" taken by the trail before the write: values come
  only from the masked read, and the unmasked one is only compared, so a
  changed secret reads `***` on both sides. A credential is recorded only as
  present, an API key never by its raw key, hash or prefix, and a number's
  call-init secret (which its read does return) and auth token are masked.
  **Append-only** twice: no route writes an entry, and a trigger refuses
  every `UPDATE` and `TRUNCATE` on the table, and every `DELETE` but the
  retention janitor's, which marks its transaction. It guards against the
  application, not the table's owner. **Retention** is the operator's
  (`AUDIT_LOG_RETENTION_DAYS`, unset = kept for good, swept hourly on every
  replica, each batch skipping rows another holds) rather
  than a project setting: an admin key must not be able to shorten the trail
  that records its own changes. **A deleted project keeps its entries**: the
  table has no foreign keys, so the ADR-0011 purge leaves the trail
  (including the entry for the deletion), and only the audit retention
  removes it; its keys stop working, so the database is where it is read
  then. The read (`GET /v1/audit-log`) is admin-only and pages by cursor,
  not page number, because entries keep arriving at the head of a
  newest-first list. The log is not redacted: it records configuration, not
  conversations.

**[[trace_include_pii]] defaults to off from the public release.** Caller
numbers in an external tracing backend should be a choice. This is a behaviour
change and goes in the changelog.

**A mapping document, not a certification.** One page that maps each control to
GDPR first, then HIPAA, and says plainly that TurnCall itself holds no
certification.

- **Mapping as built** (#318). `docs/guides/compliance.mdx` maps redaction,
  `trace_include_pii`, retention, recordings, the audit log, self-hosting,
  encrypted project credentials and signed webhooks to GDPR articles, then
  to HIPAA Security and Privacy Rule sections, and lists what stays the
  operator's: vendor DPAs and BAAs, infrastructure, access control, backups,
  the carrier's recording copy, text conversations (not under retention)
  and audio (not redacted). It cites a provision only where the control
  plainly bears on it, and says where one does not reach: redaction is not
  de-identification, the call record's numbers and metadata are neither
  redacted nor deleted by retention, and only project credentials (and a
  number's Twilio token) are encrypted by TurnCall, not call data, webhook
  signing secrets, call-init secrets or the secrets in agent config. It
  does not rely on the builder's audit of people (builder-api #101, #102),
  which is not built yet. There is no switch that turns recording off (every voice call is
  recorded, ADR-0005), so of the audio guidance above only a short
  `recordings_days` is available today, and the guides say so. The webhook guide
  (`server-events.mdx`) states what redaction does to payloads.

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
  process that redacts runs a worker process holding the loaded detector,
  ~200 MB more memory, started when a project turns redaction on or at startup
  when one has it on; a process serving no redacting project never starts it.
- What reads the conversation on the call's behalf after the words are spoken
  (the warm-transfer briefing, post-call analysis) reads it from the memory of
  the process that ran the call, never from storage. Another replica asks that
  process over Redis pub/sub to do the work; the conversation is not copied to
  Redis or Postgres to reach it (#313).
- Redaction runs before storage and before webhook dispatch, so a webhook
  consumer never sees what was redacted; that has to be stated in the webhook
  docs.
