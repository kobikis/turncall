# Changelog

Notable changes to TurnCall. Format based on [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[semantic versioning](https://semver.org/): from 1.0.0 on, a breaking change to
the REST API, the agent config schema, or the webhook payloads means a major
version bump. Breaking changes are always listed here.

History begins at 1.0.0. Earlier development predates the first public release
and is not part of this repository's history.

## [Unreleased]

### Added

- **Release numbers** (#321, ADR-0025). `POST /v1/phone-numbers/release`
  gives a number on the carrier account back to the carrier, on Twilio and
  Telnyx, admin-only (a developer or viewer key gets `403`). Releasing is
  irreversible, so it is refused with a `409` while TurnCall binds the number
  (`details.phone_number_id` names the binding when it is the caller's
  project's; unbind it first) and with a `422` unless `confirm` repeats
  `e164_number` exactly (`true` or another number is refused too). The body
  is `carrier` (`twilio` default, or `telnyx`), `e164_number`, `confirm`,
  and on Twilio an optional `twilio_account_sid` + `twilio_auth_token`
  (else the platform's `TWILIO_*` account); Telnyx uses the project's
  Telnyx account. The number is looked up on the account by E.164 (`422`
  when the account does not hold it) and deleted (Twilio
  `IncomingPhoneNumbers`, Telnyx `DELETE /v2/phone_numbers/{id}`). The
  response is `carrier`, `e164_number`, `external_number_sid`,
  `status: "released"`, `twilio_account_sid` / `twilio_credentials`. Each
  release is an audit log entry, `phone_number` / `released`, keyed by the
  E.164 number. Unbinding (`DELETE /v1/phone-numbers/{id}`) is unchanged and
  never releases. On the platform's shared carrier account (Twilio's
  `TWILIO_*` when no pair is sent; Telnyx's `TELNYX_API_KEY` when the
  project has no `telnyx` credential) a release is allowed only for a number
  the project bought there through `POST /v1/phone-numbers/purchase`;
  anything else, including another project's purchase, is a `403`
  (`forbidden`, `details.account: "platform"`) before the carrier is asked.
  A release on the project's own account is unrestricted. Purchases are
  recorded in a new `purchased_numbers` table (project, carrier, E.164,
  `platform` or `own` account; written by every purchase, pending Telnyx
  orders too, and deleted by the release). Migration `c9e3a5f7b1d4`.
  Purging a deleted project releases nothing: the purge logs a warning,
  `purged_project_left_numbers`, naming each number the project bought on
  the platform's account and never released, which stays there, billed, for
  an operator to release in the carrier's console. A Telnyx release Telnyx
  does not answer is a `502` (`telnyx_error`) saying the number may have
  been released.
- **Search and buy numbers on Telnyx** (#320, ADR-0025). The same
  `GET /v1/phone-numbers/available` and `POST /v1/phone-numbers/purchase`
  take `carrier=telnyx`, admin-only, on the project's Telnyx account (its
  `telnyx` credential, else the platform's `TELNYX_API_KEY`; neither is a
  `503`, `no_telnyx_account`). A search lists exact, voice-capable matches
  and adds `upfront_cost` / `monthly_cost` / `currency` (null on Twilio). A
  purchase places a Telnyx number order and waits up to about five seconds
  for it: `status: "active"` with `external_number_sid` = the Telnyx phone
  number id, or `status: "pending"` with it null when the order has not
  finished (bind looks the number up by E.164 and refuses it with a `422`
  until it lands). The response adds `telnyx_order_id` (null on Twilio),
  which is not the number's id. A number Telnyx will not sell is a `422`
  with `details.telnyx_codes`; a failed order a `422` with
  `details.telnyx_order_id`; a rejected API key a `422`; anything else a
  `502` (`telnyx_error`). An order that ends `cancelled` or `deleted` is a
  failed order too. An order request Telnyx does not answer (a timeout) is a
  `502` (`telnyx_error`) saying the order may have been placed, and is
  logged at error level with the number and the project. A purchase,
  pending or not, is audited as `phone_number` / `purchased`. The bought
  number binds onto the deployment's Call Control application as any other.
  No migration.
- **Search and buy numbers on Twilio** (#319, ADR-0025). An admin key
  searches the Twilio account for voice-capable numbers with
  `GET /v1/phone-numbers/available` (`carrier=twilio`, `country`,
  `number_type` of `local`, `toll_free` or `mobile`, `area_code`, `contains`
  digits, `limit` up to 50) and buys one with
  `POST /v1/phone-numbers/purchase` (`201`). The buy returns what binding
  needs: `carrier`, `e164_number`, `external_number_sid` (the PN SID),
  `status`, and `twilio_account_sid` / `twilio_credentials`. Twilio has no
  project credential, so both run on the Twilio account the request names
  (`twilio_account_sid` + `twilio_auth_token` in the buy's body, the
  `X-Twilio-Account-Sid` + `X-Twilio-Auth-Token` headers on the search) or
  on the platform's `TWILIO_*` account; bind with the same pair. A developer
  or viewer key gets `403`; no account at all is a `503`
  (`no_twilio_account`); a number Twilio will not sell is a `422` with
  `details.twilio_code`; a number already bound is a `409`. Each purchase is
  an audit log entry, `phone_number` / `purchased`, keyed by the E.164
  number. Buying binds nothing: the number is bound with
  `POST /v1/phone-numbers` as before. No migration. The carrier sells the
  number before TurnCall records it, so if recording, auditing or the
  commit then fails, on either carrier, the purchase is a `500`
  (`internal_error`) whose `details.bought` names the number, logged at
  error level (`phone_number_bought_untracked`) with the carrier, E.164,
  number id or Telnyx order, and project.
- **Audit log** (#317, ADR-0024). Every change made through the API to a
  project's agents (create, update, delete, publish, rollback, knowledge base
  links, and the versions a publish or rollback archives), credentials, phone
  numbers (including A/B routing, and the numbers a publish or rollback
  re-points), API keys and project settings (limits, redaction, retention,
  deletion) is an entry: the API key that made it (or `actor: platform` for
  the platform credential's bootstrap of a project and its first key), the
  resource, the action, when, and what changed, field by field before and
  after, as the API reads it. No secret is recorded: a credential's value, an
  API key's raw key, hash and prefix, the secrets the agent read masks, and a
  number's `server_url_secret` and auth token read `***`, so a changed secret
  shows as changed and nothing more. Each changed field is named by its
  JSON Pointer (`/config/llm/model`). The entry is written in the change's
  transaction; a request on these routes that would commit without one is
  refused with a `500` and rolled back. `GET /v1/audit-log` (admin key; a
  developer or viewer key gets `403`) reads the project's entries newest
  first, filtered by `resource_type`, `resource_id`, `action`, `request_id`,
  `since` and `until`, paged by `cursor`/`next_cursor`. Each audited
  response names its primary entry in the new `X-TurnCall-Audit-Id` header,
  so a client such as the builder can link to it; that id is the
  `request_id` of every entry the request wrote, so `?request_id=` reads
  back a publish's archived versions and re-pointed numbers too. Append-only: no route writes
  an entry, and a database trigger refuses every `UPDATE`, `TRUNCATE` and
  `DELETE` but the retention janitor's. Retention is the operator's new
  `AUDIT_LOG_RETENTION_DAYS` (unset or empty = kept for good), swept hourly; call
  retention never touches the log, and a deleted project's entries outlive
  its purge. Migration `b3d7f1a9c5e2`. See the Audit log guide.

- **Retention** (#316, ADR-0024). A project can choose how long its calls'
  recordings, transcripts and events are kept: `PUT
  /v1/projects/{id}/retention` (admin key) with `recordings_days`,
  `transcripts_days` and `events_days`, whole days from the call's end, `null`
  (the default) to keep for good. An agent overrides any of them with an
  additive `retention` block in its config, read from its live version and
  applied to the calls of all its versions; an inline agent's calls take the
  project's. Changing what applies to an agent needs an admin key too: a
  developer key that writes a different `retention`, or publishes or rolls
  back to one, gets a `403`. A janitor on every API server deletes what has
  expired, hourly: a recording's object in storage, its copy on the call's
  Twilio account when a `recording.ready` names one, and its
  `recording_url`, with `recording_status` set to the new value `expired`
  and the URLs on `recording.ready` cleared; a transcript's lines, tool
  events and tool-invocation rows, injected messages, the post-call analysis
  with its takeaways, and the free-text fields other events quote (a
  handoff's reason and context, a transfer's reason and message); and the
  rest of the call's event log (`recording.ready` stays while the recording
  does). A carrier recording TurnCall never heard about, or on an account it
  holds no credentials for, it cannot delete: your carrier may keep its own
  copy; delete it there or turn carrier recording storage off. The call
  record stays, with `recording_expired_at`,
  `transcript_expired_at` and `events_expired_at` on the call read saying what
  went, and `ended_reason` kept. `GET /v1/calls/{id}/transcript` gains
  `expired_at`, `GET /v1/calls/{id}/analysis` answers `status: "expired"`, and
  re-running the analysis of an expired transcript is a `409` (a re-run
  already under way when it expires stores nothing and sends no
  `call.ended`). `call.ended`
  carries `retention`, what applies to the call. A change applies to calls
  already stored. Text conversations and the audit log are not touched.
  Migration `e2c6a8f4b9d1` (three partial indexes on `calls`, built
  `CONCURRENTLY` so writes to `calls` never wait on them, and `expired` on
  the `recording_status` enum). See the Retention guide.

- **Redaction** (#313, ADR-0024). A project can have PII replaced in
  everything it stores and sends about its calls: `PUT
  /v1/projects/{id}/redaction` (admin key) with `{"enabled": true}` and,
  optionally, `entities` — Presidio's type names, `PHONE_NUMBER`,
  `EMAIL_ADDRESS`, `CREDIT_CARD`, `US_SSN` and `IBAN_CODE` by default,
  `PERSON` and the rest opt-in. A detected value becomes `<ENTITY_TYPE>` in the
  transcript, on the call's event log and in every webhook payload, before it
  is stored or sent, so a subscriber never sees the original. With
  `PHONE_NUMBER` chosen, the payloads' number fields are replaced too
  ([which, and when](docs/guides/redaction.mdx#number-fields)); the call
  record keeps the number, so `GET /v1/calls/{id}`,
  routing, the suppression list and campaigns are unaffected. The agent is
  never redacted: the model hears the caller's words, a warm transfer's
  operator is briefed on them, and post-call analysis and takeaways read them.
  Their results are redacted before they are stored on the call, so `analysis`
  on the call read, `GET /v1/calls/{id}/analysis` and `call.ended` agree
  (tool invocations, text conversations, logs and spans: below, #314).
  TurnCall's own identifiers (ids, statuses, provider and tool names)
  are never analysed. Detection is local — Presidio over spaCy's small English
  model, no text leaves the deployment — and runs in a worker process of its
  own, outside the process carrying live calls' audio; a project with
  redaction off never starts it. It loads in the background when a project
  turns redaction on, and at startup when any project has it on. If it cannot
  load, analysed text is withheld (`<REDACTED>`) for five minutes before it is
  tried again; if it falls far behind, text is withheld rather than queued.
  `enabled` is required in the `PUT`. Off for every project until turned on;
  a change applies at once on the server that took it and within 30 seconds
  on the others, and nothing stored before it is rewritten. Recall is the detector's:
  digits spoken as words ("four one five…") are not caught, so an STT that
  does not format numbers (Deepgram's `smart_format` does) leaves them.
  Migration `d7f2a4c9e5b8`. See the Redaction guide.
- **Redaction of text conversations** (#314): SMS, WhatsApp and the Chat API.
  A text session's model rereads the stored messages every turn, so they stay
  as written while the session is live; with redaction on, every read of them
  is redacted — `GET /v1/chat/sessions[/{id}]` (`customer_number`,
  `turncall_number`), `GET /v1/chat/sessions/{id}/messages` and
  `POST /v1/eval-scenarios/from-session`. A session that ends while its
  project redacts (its 24-hour expiry, `DELETE /v1/chat/sessions/{id}`, or a
  new session replacing it) has its messages and numbers rewritten redacted in
  the database by the session sweep, within 15 minutes. A session that ended
  before redaction was turned on is not rewritten. The reply to
  `POST /v1/chat` and the text sent to the customer are the live conversation
  and are never redacted. Every API server runs the sweep and claims the
  sessions it rewrites (a ten-minute lease), so none is rewritten twice; a
  reply stored as its session is rewritten puts it back for the next sweep.
  The state is kept in `metadata_json`; migration `b5e1d9c3a7f2` adds a
  partial index for the sweep's claim. A page of
  `GET /v1/chat/sessions/{id}/messages` or `GET /v1/chat/sessions` now holds
  at most 500: a larger `limit` is cut to 500, which the response's `limit`
  reports (a page is redacted before it is answered, in jobs of 64 texts).

- **Redaction: tool invocations** (#314). With redaction on, a tool call's
  arguments and result are stored redacted on its `tool_invocations` row, so
  `GET /v1/tools/invocations/{call_id}`, a chat session's tool invocations
  and scenarios drafted from a call or session read back redacted. The fields
  are the tool's own, so every value is analysed whatever it is called (a
  `customer_id` holding an email is redacted); the same holds for takeaway
  and `structured_data` fields in the stored analysis. A JSON number of
  seven digits or more is analysed as its digits, so
  `{"card_number": 4111111111111111}` is stored as `"<CREDIT_CARD>"`. The
  tool's webhook is
  still sent the raw arguments and the model still reads the raw result.

- **Redaction in logs and traces** (#314). A redacting project's log lines
  and OpenTelemetry spans are redacted before they leave the process: the
  carrier webhooks' caller numbers, the pipeline observers' transcripts, the
  STT/LLM/TTS spans' text and the model's input and output, tool calls,
  transfers, post-call analysis, SMS and WhatsApp messages, and every request
  made with the project's API key. Regular expressions rather than the
  detector, which is too slow for the audio path: `PHONE_NUMBER`,
  `EMAIL_ADDRESS`, `CREDIT_CARD`, `US_SSN` and `IBAN_CODE`, as the project
  chose them; other types are not found in logs and spans. Numbers side by
  side are found one by one, a number inside an email-shaped string (a
  WhatsApp JID) is found when phone numbers are chosen and emails are not,
  and epoch timestamps and zero-padded ids are left alone: a bare run of
  digits starting with 1 is a phone number only at 11 digits, a card number
  only at 15. For such a
  project a logged exception's traceback is written without its local
  variables, redacted, in the message (JSON `exception` is `null`), and the
  call's `turncall.from_number` / `turncall.to_number` are left off its spans
  whatever `PIPECAT_TRACE_INCLUDE_PII` says. A project with redaction off logs
  as before. See the Redaction guide.

- **Helm chart** (#210), in its own repo,
  [turncall-charts](https://github.com/kobikis/turncall-charts). Runs the API, the eval worker and
  the campaign worker against your own Postgres and Redis, with migrations as a
  hook Job before each install and upgrade. Each grace period is sized from what
  that process may still be holding — the API's from your longest
  `max_call_duration_seconds` (`maxCallDurationSeconds`) — so a scale-down or a
  rollout lets live calls finish instead of cutting them off. An optional
  `HorizontalPodAutoscaler` scales the API on live calls per pod
  (`turncall_active_calls`) rather than CPU. Asterisk's ARI connection runs in
  one dedicated pod, never in an API replica, since ARI gives an app's calls
  to one connection only. The same chart can run the builder beside TurnCall
  (`builder.enabled`). See the Kubernetes guide.
- **Everything in the cluster** (the chart's `values-local.yaml`). The
  chart can also run its own Postgres (pgvector) and Redis
  (`postgresql.enabled`, `redis.enabled`) and keep files on a volume
  (`localStorage.enabled`) — the docker compose stack on Docker Desktop, kind
  or minikube. It fills `DATABASE_URL` and `REDIS_URL` itself, and migrates in
  the API pod's init container, since a pre-install hook runs before the
  database the same install creates; the workers wait for that migration
  instead of crashing until it lands. For development: one pod each, no
  backups. `publicBaseUrl` sets `PUBLIC_BASE_URL` (and the builder's webhook
  display), and the install notes warn when it is unset,
  since a number bound without it is saved but not configured at the carrier.
- **Deepgram Flux STT** (#204). A `flux-*` model on the `deepgram` provider
  (`flux-general-en`, `flux-general-multi`) uses Deepgram's Flux, which detects
  the end of the caller's turn itself. Flux then owns end-of-turn: Smart Turn
  is not run for that agent and `silence_timeout_ms` is not used, so the two
  waits are not stacked. Flux's `eot_threshold`/`eot_timeout_ms` and its other
  settings go in `stt.extra`. Same Deepgram key.
- **`metrics.turn` carries `turn_end_ms`** (#204): the time from the caller going
  quiet to the turn being called over — VAD, the transcript, and Smart Turn's or
  Flux's verdict. Absent when unmeasured, never zero.

- **Warm transfer on Telnyx numbers** (#221, ADR-0009, ADR-0022). The
  operator hears the `briefing` — a string or `{"from_summary": true}`,
  summarized when they pick up — spoken on their own leg before the caller is
  connected, while the caller hears ringing. Machine detection on the
  operator's line reports `transfer.answered` (`human`/`machine`/`unknown`) and
  never holds the bridge; an operator who does not answer gets the caller the
  `fallback_message` and the call ends. The carrier contract runs the same
  three warm scenarios — answered, voicemail, no answer — on Twilio and Telnyx.
- **Cold transfer and DTMF on Telnyx numbers** (#220, ADR-0009, ADR-0022).
  `transfer_call` and `POST /v1/calls/{id}/transfer` behave on a Telnyx call as
  on Twilio: the caller hears the `transfer_message`, then ringing, then the
  operator, whose caller ID shows the caller's number; if nobody answers, the
  call ends. On Call Control that is a sequence of commands driven by Telnyx's
  webhooks (stop the agent's stream, speak, ringback, dial a linked leg,
  bridge). `send_dtmf` sends its digits with Telnyx's `send_dtmf` command.
  The carrier contract test runs the cold transfer and DTMF on both carriers.

- **Telnyx numbers answer inbound calls** (#217, ADR-0022). Bind a number with
  `"carrier": "telnyx"` (routed for you since #219, below): the call is answered through Call Control, its
  audio streamed with pipecat's Telnyx serializer, and it runs the same agent
  config as on Twilio — recording, ambience and voicemail detection included.
  Webhooks are verified with the account's Ed25519 public key and refused
  without a valid signature; there is no unsigned mode. The account is a
  `telnyx` project credential (`api_key` + `public_key`) over the platform's
  `TELNYX_API_KEY` / `TELNYX_PUBLIC_KEY`; with no key at any level the call
  fails with `missing_keys: ["telnyx"]`. A call over the project's live-call
  limit (#224) is refused busy with a Call Control `reject` and announced as
  `call.rejected`, as on Twilio.
- **Outbound calls from Telnyx numbers** (#218). `POST /v1/calls/outbound`
  with a Telnyx number as `from_number_id` dials through Telnyx Call Control —
  same request, same response, and the same suppression list and limits, so
  campaigns dial Telnyx numbers too. The dial runs on the number's Call Control
  application and streams the call once the callee answers. A call nobody
  answers ends `customer_did_not_answer` (busy: `customer_busy`), as on Twilio.
  No Telnyx key at any level is a `503 no_telnyx_account` naming
  `missing_keys: ["telnyx"]`; a number on no Call Control application is a 409.
- **Binding a Telnyx number routes it** (#219). No Mission Control step: the
  bind looks the number up on the project's Telnyx account (so
  `external_number_sid` is optional, and a number the account does not hold
  is a 422 naming it) and moves it onto this deployment's Call Control
  application, `TurnCall <host>`, which TurnCall creates, keeps pointed at
  `/webhooks/telnyx/voice`, and deletes once no number is left on it. Unbind
  and project deletion take the number off again. Bind and update responses
  carry `webhooks_configured` for any carrier.

### Changed

- **Caller numbers are off spans by default** (#315, ADR-0024).
  `PIPECAT_TRACE_INCLUDE_PII` now defaults to `false`: caller numbers in an
  external tracing backend are a choice, not a default. The non-PII attributes
  (`project_id`, `agent_id`, `call_sid`, `direction`, `transport`) are
  unchanged, and a project with redaction on still never gets the numbers on
  its spans. See Upgrade notes.
- **The image carries a PII detector** (#313): Presidio, spaCy and its small
  English model (`en_core_web_sm`, pinned in `pyproject.toml`), ~255 MB
  unpacked and ~74 MB of the pull. With it, numpy resolves to 2.4.x
  (presidio-analyzer caps it below 2.5) and `presidio-anonymizer` is pinned to
  2.2.362 (2.2.364 caps cryptography below 49). Neither of its runtime
  downloads runs: a missing model is an error, not a pip install, and the
  image sets `TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS=""` so the email recognizer
  uses its bundled suffix list instead of fetching one.
- **The warm-transfer briefing and post-call analysis read the conversation
  from memory** (#313). The process that ran the call keeps what was said for
  five minutes after it ends, and the briefing summary, analysis and takeaways
  read that rather than the stored transcript, so redaction never reaches
  them. For a project that redacts, when another replica handles the
  carrier's whisper or status callback or the end API, it asks the process
  holding the conversation (Redis pub/sub, naming only the call) to build the
  briefing or run post-call processing; the conversation itself is never
  written to Redis or the database. Only when no process holds it any more (a
  restart, after five minutes, a re-run) do they read the stored transcript,
  redacted, and log that they did.
- **A production WebRTC `server_url` may be a Kubernetes Service**
  (`http://<name>.<namespace>.svc[.cluster.local]`). It had to be HTTPS, which
  refused a call-init server in the same cluster — plain HTTP there never
  leaves it. Any other `http://` URL is still refused in production, and the
  suffix must be the host's.
- **A phone number's carrier is `carrier` in the API** (#201, ADR-0022).
  Reads carry `carrier`; `provider` is still there with the same value and
  still accepted on a write, both deprecated — "provider" means an STT, LLM or
  TTS backend everywhere else. Sending both with different values is a 422, as
  is a `PUT` that would move a number to another carrier. Behind it the
  carrier boundary is cut for Telnyx and Asterisk: the telephony adapter is
  REST actions only, and one media entry point picks the serializer from the
  call's own carrier. Twilio calls behave exactly as before.

- **An `openai_live` agent with tools now delegates to its own `llm`** (#244).
  With no `s2s.delegation` block its tools were never offered; it now gets
  client delegation, so the `llm` provider needs a key (`missing_keys`
  otherwise). `end_call`, `do_not_call` and `transfer_call` work from the
  client delegate (#245): the call ends after gpt-live-1's goodbye, as on
  cascade.

- **`handoff_to_agent` on an `openai_live` S2S agent is now refused** (422)
  (#243). The handoff swapped the delegate's tools but not gpt-live-1's
  session, so the caller kept the previous agent's voice and prompt.

- **`end_call` and `do_not_call` let the agent finish its goodbye.** The tool
  used to hang up over the carrier's API the moment it ran, cutting off what
  the agent was still saying. It now records the end and lets the model answer
  the tool's result; the call ends once that reply — the goodbye — has played
  out. Instructions that ask the model to say goodbye *before* calling the tool
  now work against it: many models then say goodbye and wait for the caller.
  The goodbye is final: the caller cannot interrupt it, and what they say
  while it plays is not transcribed or recorded.
  On WebRTC, where `end_call` used to mark the call ended and leave it
  running, it now ends it. `POST /v1/calls/{id}/end` still hangs up at once.

- **Two credential failures have their own error codes** (#191). A 503 from
  storing a credential or binding a number's own Twilio account while
  `CREDENTIAL_ENCRYPTION_KEY` is unset or wrong is now
  `credential_key_unavailable`, and an outbound call from a number with no
  Twilio account anywhere is `no_twilio_account`. Both were `internal_error`.

- **A call that fails reads as failed, and says why.** A provider that stops
  working mid-call (a refused key, an unknown model) used to end the pipeline
  gracefully, so the call finished `completed` with an `ended_reason` of
  `customer_ended_call`. It now ends `failed` / `pipeline_error`, and the call's
  event log carries an `error.raised` event, `{stage, error}`, also dispatched
  to webhook subscribers. A key that is present but refused adds `provider` and
  `key_rejected: true`. If you alert on `customer_ended_call`, calls that broke
  on a provider no longer land there.

- **A provider with no key at any level is refused before the call starts.**
  Every provider the agent names is checked (agent inline, project credential,
  platform environment) before the pipeline is built, on every transport. A
  missing one fails the call with `error.raised` carrying
  `missing_keys: [account]`, instead of one SDK's "Missing credentials" or a
  401 partway into the greeting. `POST /v1/webrtc/connect` answers
  `500 pipeline_start_failed` with the same list in `details.missing_keys`.
  Eval runs are exempt: text mode opens no speech provider.

- **`AWS_AGENT_CREDENTIALS_ENABLED` governs agent config only** (#164). With it
  false, static AWS keys are still refused inside an agent's `aws` block, but an
  `aws` project credential is accepted — it is encrypted and outside agent
  config. False no longer means "no static AWS keys on this deployment".

### Added

- **AssemblyAI Universal-Streaming STT** (#203): `"stt": {"provider":
  "assemblyai"}`. The key resolves agent → project `assemblyai` credential →
  `ASSEMBLYAI_API_KEY`, and a missing one is reported in `missing_keys` like
  any other provider. With no `stt.model` it runs `universal-streaming-english`
  ($0.15/hr) — or `universal-streaming-multilingual` when `stt.language` is not
  English, since the English model transcribes other languages as English words
  without an error. `stt.keyterms` is sent as `keyterms_prompt`, capped at the
  100 AssemblyAI accepts (more is a refused session, not a weaker one), and
  `stt.extra` reaches AssemblyAI's own settings (`domain`, `mode`, …).

- **`google` LLM provider: Gemini as a cascade LLM** (#205), on voice and
  chat/SMS/WhatsApp text, tools included, on the `google` account Gemini Live
  already uses (`GOOGLE_API_KEY` or a project credential). House model
  `gemini-3.6-flash`. Gemini 3 models are sent no temperature — Google warns
  they loop below their own 1.0, and TurnCall's 0.7 is a default nobody chose;
  `llm.extra` merges into the generation config to set one anyway. Usable as
  an `llm.failover` entry; `base_url` is refused (422).

- **A deploy no longer drops live calls** (#199). On `SIGTERM`, `GET /ready`
  answers `503 {"status": "draining"}` at once while `GET /live` stays `200`,
  and the process keeps serving until its live calls (Twilio, WebRTC and
  WhatsApp voice) have ended and their `call.ended` webhooks have gone out —
  bounded by the longest `max_call_duration_seconds` among them, plus 90
  seconds. Before, the server's own shutdown closed every media stream at once
  and cut each call off mid-sentence. A second `SIGTERM` stops waiting. Set
  `terminationGracePeriodSeconds` from your agents' durations, not the 30-second
  default; see the Graceful shutdown guide. `GET /metrics` exposes
  `turncall_active_calls`, live calls per process, in Prometheus text format.
- **Weighted agents and start/finish events on campaigns** (#234). A campaign
  takes `agents: [{agent_id, weight}]` (weights summing to 100) instead of
  `agent_id`, and each target is assigned its agent at create by the phone
  number A/B rule — a hash of its number, so the same list splits the same way
  every time — readable as the target's `agent_id`. Webhook subscribers get
  `campaign.started` and `campaign.completed`, each carrying the campaign's
  read (status and target counts), with the campaign's id in a new nullable
  envelope field, `campaign_id`. A weighted campaign reads back `agent_id: null`.

- **Campaigns keep a number's pace and are never refused by a limit** (#233).
  The worker reserves each dial's live-call slot and calls-per-second place
  before it claims a target, so two workers — or a worker and an API dial —
  racing one project no longer end in a `429` for the campaign; and it comes
  back as soon as the number's one-second window frees, so a campaign dials
  *at* `outbound_calls_per_second` rather than once per poll. With no rate set
  on the project, a campaign paces at `CAMPAIGN_DEFAULT_CALLS_PER_SECOND`
  (default `1`, Twilio's default account rate; `0` turns it off) instead of
  dialling the whole list in one burst.
- **Campaign pause, resume and cancel; a worker crash loses no target** (#232).
  `POST /v1/campaigns/{id}/pause` stops new dials and leaves calls already
  placed up — their endings are still recorded — and `/resume` carries on
  where it stopped. `/cancel` ends the campaign: targets not yet dialled become
  `cancelled`, live calls finish, and subscribers get `campaign.cancelled`
  (the same payload as `campaign.completed`). A move that lands between a worker's claim
  and its dial still stops that dial. A worker killed mid-dial is recovered by
  a janitor in the next worker's pass (`CAMPAIGN_CLAIM_TIMEOUT_SECONDS`,
  default 120): a target claimed but not yet sent to the carrier is dialled
  again, and one the carrier was being asked about — a new `placing` status —
  is ended `failed` with `dial_outcome_unknown` instead, because it may already
  have rung and calling a person twice is worse than reporting one call as
  unknown.

- **Campaigns: outbound calls to a list** (#229). `POST /v1/campaigns` takes
  one agent, a caller-id number and JSON targets — a number, per-target
  `variables` (rendered into the agent as `{{name}}`, like call-init's) and an
  IANA `timezone`, on each target or as the campaign default. A target with
  neither is refused at create: its local time is what a calling window will be
  read in, and a zone guessed from the number calls people at night. A new
  process, `turncall-campaign-worker`, dials each target once through the same
  dial path as `POST /v1/calls/outbound`, so suppressed numbers are skipped
  (and marked `suppressed`) and the project's live-call limit is never
  exceeded. `GET /v1/campaigns/{id}/targets` gives each target's status, call
  and `ended_reason`. Calling windows, retries, pause/cancel, pacing and
  weighted agents follow (#230-#234).

- **Campaign calling windows** (#230). `calling_window: {start, end, days}` on
  `POST /v1/campaigns` — local hours, read in **each target's own** IANA zone
  on the day in question, so a New York target's 09:00 is 13:00 UTC in October
  and 14:00 UTC after the November change. A target reached outside its window
  goes back to `pending` with `next_attempt_at` at its next opening; the check
  sits on the claimed target right before the dial, so nothing reaches the
  carrier outside it. `days` defaults to every day; an overnight window or a
  time with an offset is a 422. No window, no restriction — as before.

- **Campaign retries by `ended_reason`** (#231). `retry_policy` on
  `POST /v1/campaigns` maps `customer_did_not_answer`, `customer_busy` and
  `voicemail` each to `{max_attempts, spacing_seconds}`. A call ending that way
  puts its target back to `pending`, due after the spacing — moved to the next
  opening of its calling window when it lands outside — until the target's
  `attempts` (dials placed, the first included) reach that ending's
  `max_attempts`. Every other ending is final: a person who answered is not
  called back. No policy, no retries — as before.

- **A messages-per-minute limit per text session** (#226).
  `messages_per_minute` on `PUT /v1/projects/{id}/limits`, with
  `LIMIT_MESSAGES_PER_MINUTE` as the platform default — unlimited unless set —
  so a looping or abusive conversation cannot run up LLM cost. Past it a
  message is neither stored nor sent to the LLM: `POST /v1/chat` answers
  `429 rate_limited` with `retry_after_ms`, and an SMS or WhatsApp message gets
  no reply. Counted per session, across every server.

- **An outbound calls-per-second limit per number** (#225).
  `outbound_calls_per_second` on `PUT /v1/projects/{id}/limits`, with
  `LIMIT_OUTBOUND_CALLS_PER_SECOND` as the platform default — unlimited unless
  set. Each of the project's numbers starts at most that many outbound calls
  in any one second, across every server; a dial past it is
  `429 rate_limited` with `retry_after_ms`, and never reaches the carrier.

- **A live-call limit per project** (#224). `PUT /v1/projects/{id}/limits`
  `{"live_calls": n}` (admin key; the project read shows `limits`), with
  `LIMIT_LIVE_CALLS` as the platform default — unlimited unless set. Over it,
  an inbound Twilio or WhatsApp call is refused unanswered at the carrier
  (busy) and dispatched as the new `call.rejected` event (no `call_id`: the
  call never started); `POST /v1/calls/outbound` and `POST /v1/webrtc/connect`
  answer `429 rate_limited`. Counted in Redis across every server. A call
  frees its slot however it ends, and one whose server died frees it within
  two minutes.
- **Asterisk numbers, over ARI** (#209, ADR-0022). A call centre or company
  PBX reaches an agent with no cloud carrier in between: bind a number with
  `carrier: "asterisk"`, give the project its Asterisk as an `asterisk`
  credential (`ari_url`, `username`, `password`) or set `ASTERISK_ARI_*` for
  the platform, and point the dialplan at `Stasis(turncall)`. TurnCall keeps
  an ARI connection to it, takes the call's audio over the WebSocket channel
  driver (Asterisk 20.18 / 22.8 / 23.2 or later) and runs the same pipeline as
  on Twilio. Hang-up, DTMF and cold transfers (into a dialplan context) go
  through ARI; a warm transfer, a transfer or fallback message, outbound
  calls and SMS are refused on Asterisk numbers rather than done differently.
  A call TurnCall cannot take goes back to the dialplan unanswered — one over
  the project's live-call limit (#224) too, with `call.rejected`. One
  replica holds the ARI connection (`ASTERISK_EDGE_ENABLED`). See
  [Phone numbers](docs/guides/phone-numbers.mdx#asterisk-numbers).

- **`make load-test`: how many concurrent calls one pod carries** (#200,
  `scripts/calls_per_pod/`). Runs the app in the API image under a pod's CPU
  and memory budget, on the harness's stub providers, and ramps N real-time
  Twilio media streams playing recorded speech. Per step: CPU (process and
  event-loop thread), CFS throttling, RSS, loop lag, full-GC pauses, audio
  underruns as a caller's playout buffer hears them, and turn latency. The
  measured figure and its conditions are in `docs/guides/capacity.mdx`.

- **A test harness that drives the running app like a carrier** (#214,
  `tests/harness/`). Stub STT/LLM/TTS answer instantly from a script (a tool
  call included), a fake carrier records every REST action TurnCall takes, and
  a Twilio client posts signed webhooks and streams real-time mulaw media.
  `tests/integration/test_harness_inbound_call.py` runs one inbound call from
  webhook to `call.ended` on it. Later carrier, limit and campaign tests extend
  it rather than mock their own way in.

- **`stt.failover` and `tts.failover`** (#223). Ordered backup STT and TTS
  configs, each validated like `stt`/`tts`. An error that leaves the active
  service unusable moves the call to the next and it carries on: the STT
  backup is handed the caller's speech nobody transcribed yet, the TTS backup
  the reply being spoken. Recoverable errors do not switch; there is no
  switching back. Each switch is `provider.failover` (`role: stt|tts`) and an
  entry in `failovers`. Backups need keys (`missing_keys`). 422 on S2S agents,
  and on an STT group mixing Deepgram Flux with other models.

- **`llm.failover` on an `openai_live` client delegate** (#246). The delegate
  is an ordinary LLM, so it is built as a failover group like cascade's: an
  unusable error moves the delegation to the next backup, recorded as
  `provider.failover` and in `failovers`. Backups need keys (`missing_keys`).
  Still a 422 on every other S2S agent, responses mode included.

- **Client delegation for `openai_live`** (#244): `s2s.delegation`
  `{"mode": "client"}` makes the agent's own `llm` gpt-live-1's delegate, run
  with the agent's static and MCP tools and TurnCall's handlers — mocked in
  evals, recorded in `tool_invocations`. `instructions` and `timeout_seconds`
  (one delegation's budget, default 30) apply; `model`, `reasoning_effort` and
  `service_tier` are responses-only and a 422 here.

- **`s2s.delegation` on `openai_live` agents** (#243). gpt-live-1 calls no
  tools itself; it hands them to a *delegate*. The new block configures a
  Responses delegate (`mode: "responses"`, `model`, `reasoning_effort`,
  `service_tier`, `instructions`, `timeout_seconds` default 30), and gpt-live-1
  is now told to delegate, from the agent's own tool names and descriptions.
  `s2s.extra.backend_model` keeps working, read as that block. Refused with a
  422: `delegation` on any other S2S provider, responses mode without `model`.
  An agent on `backend_model` now gets the same 30-second default: each
  function call the delegate makes is cancelled past it (it was unbounded),
  so a webhook tool whose own timeout × retries runs longer needs
  `timeout_seconds` raised. `delegation.instructions` takes call-init
  `{{variables}}` like `system_prompt`, but not `knowledge_context`, which is
  prepended to `system_prompt` only.

- **A `do_not_call` built-in tool** (#228). Listed in an agent's `tools`, it
  adds the caller's number to the project's suppression list, records
  `call.opted_out` (`{number}`, dispatched to webhook subscribers) and ends the
  call. Absent unless listed. A call with no phone number is refused and left
  running.

- **A suppression list, enforced on every outbound dial** (#227).
  `POST/GET /v1/suppression-list` and `DELETE /v1/suppression-list/{id}`
  manage a project's numbers that must never be dialled; an outbound call to
  one is refused `409 number_suppressed` before any call row or carrier dial.
  Adding a listed number again returns the existing entry. No consent records.

- **A phone number can bring its own Twilio account.** `POST /v1/phone-numbers`
  and `PUT /v1/phone-numbers/{id}` take `twilio_account_sid` +
  `twilio_auth_token`. The number is looked up on that account — so
  `external_number_sid` is optional, and credentials that do not own the number
  are refused (422) before anything is bound. The token is stored encrypted and
  never returned; the read gains `twilio_account_sid` and `twilio_credentials`
  (`number` | `platform`). Webhook configuration, outbound calls, hangup,
  transfer, DTMF, recording download and inbound signature checks for that
  number all use its account. Numbers without one behave exactly as before.
  With no platform Twilio account, an inbound webhook TurnCall cannot verify
  passes only while no number carries its own account; once any does, it is
  refused — removing `TWILIO_*` from the environment never turns the check off.

- **Project credentials — a project runs on its own provider accounts** (#159,
  ADR-0020). `PUT /v1/credentials/{provider}` stores one key per provider
  *account* (Deepgram once for STT and TTS, OpenAI once for four roles; `aws`
  takes the agent AWS block's credential set), Fernet-encrypted at rest;
  `GET /v1/credentials` and `DELETE /v1/credentials/{provider}` list and revoke.
  No read returns a value. Every call resolves each provider's key as agent
  inline → project credential → platform environment, read fresh per call, so a
  write or a revocation takes effect on the next call with no restart.
  `GET /v1/providers` lists the valid providers per role and, per account, two
  booleans: `platform_key` and `project_credential`. New setting:
  `CREDENTIAL_ENCRYPTION_KEY` (set once; startup fails if credentials exist and
  it is unset). Deepgram, ElevenLabs, HeyGen and Tavus keys are now read
  through settings like every other provider's. The same order applies outside
  calls — chat/SMS/WhatsApp text turns, post-call analysis and takeaways, the
  transfer briefing, knowledge-base embeddings and the eval judge — so the
  platform `.env` keys are a fallback, never a requirement. `typesafe` (the
  `jev` eval judge) is an account too, so `TYPESAFE_API_KEY` is a fallback like
  the rest.

- **Ambience — a room the agent sounds like it is in** (#166). Upload a sound
  once (`POST /v1/ambience-sounds`) and any agent in the project names it:
  `"ambience": {"sound": "<id>", "volume": 0.3}`. It plays quietly under and
  between everything the agent says, for the whole call, on Twilio, WebRTC and
  WhatsApp voice — S2S included. Each upload is decoded, forced to mono and
  stored once per transport rate, because the mixer plays nothing at any other
  shape and says nothing about it; the read returns a URL per rendition so you
  can hear what a caller will. 10 MB / 60 s caps; `.m4a` is refused with the
  conversion that fixes it. A sound is refused deletion (409, naming the
  agents) while any agent uses it. Off unless configured, never in evals (the
  run says so in `warnings`), and never able to end a call. New setting:
  `AMBIENCE_CACHE_DIR`. See `adr/0021`. Checked on a real PSTN call: no
  spurious interruptions and no empty turns at the default volume (#171).

### Security

- **A carrier media stream must prove it is the call's.** Every media route —
  Twilio's `/ws/media-stream`, and Telnyx's and Asterisk's added in this
  release — built the call's real pipeline on any socket that named its call
  id, on any carrier's route, as many times as asked. A call id is not a
  secret (it rides in webhooks, call-init, the API and logs), so whoever held
  one could talk to the caller's agent, hear its replies and drive its tools.
  Each call's stream setup now carries a **media token** (an HMAC of the call
  id keyed by `API_KEY_HASH_SECRET` — set it; the default is public), and a
  stream without it, on another carrier's route than the call's, or for a call
  that already has one is refused before anything is built. Telnyx's stream
  path gains a segment (`/ws/telnyx/media-stream/{call_id}/{token}`) and
  Asterisk's external-media channel id the token; both are minted by TurnCall,
  so no carrier configuration changes. Migration `b9f5d3a7e1c2`
  (`calls.media_attached_at`). A call answered before the upgrade and not yet
  streaming when it lands has no token and is refused.
- **Twilio webhooks no token can verify are refused outside development**
  (#337). With no `TWILIO_AUTH_TOKEN` and no number on its own Twilio account,
  every `/webhooks/twilio/*` route took a request unverified, so a production
  deployment whose token was unset or blank accepted forged calls and texts.
  Such a webhook is now taken only when `TURNCALL_ENV` is `development`;
  every other environment, staging included, answers `403` before anything
  is read or created, and logs `twilio_webhook_unverifiable` (once a minute
  per route, with how many it left out: anyone can cause it). A token that is
  only whitespace counts as unset. `/webhooks/twilio/stream-status`, the one
  route that never checked a signature, now does, against the same URL the
  TwiML gave Twilio: both are built from one origin, and in production that
  is always `https`, behind a TLS terminator that sends no
  `X-Forwarded-Proto` too. At startup, Twilio numbers on the platform account
  while `TWILIO_AUTH_TOKEN` is unset are logged as
  `twilio_numbers_unverifiable`, with whether their webhooks are refused, and
  plainly when they are accepted only because `TURNCALL_ENV` was never set.
  See the upgrade notes.

- **A WhatsApp webhook nothing can verify is refused outside development**
  (#339). Without `WHATSAPP_APP_SECRET`, every WhatsApp webhook (voice calls
  and text messages) was accepted unverified, in production too. It is now
  accepted only when `TURNCALL_ENV` is `development`; every other
  environment, staging included, refuses it with a `403` before anything is
  read, logged once a minute. A secret that is only whitespace counts as
  unset. Startup warns when WhatsApp is configured (`WHATSAPP_TOKEN`,
  `WHATSAPP_PHONE_NUMBER_ID` or `WHATSAPP_WEBHOOK_VERIFY_TOKEN` set, or a
  `whatsapp_enabled` number) without it. Meta's subscription handshake
  (`GET`, `hub.challenge`) is unaffected. See the upgrade notes.

- **ICE trickle needs the key that opened the connection** (#342).
  `PATCH /v1/webrtc/connect` took no API key and checked nothing but the
  body's `pc_id`, which is not a secret (it shows in logs, browser
  extensions, shared screens), so whoever learned a live call's `pc_id` could
  add ICE candidates to its peer connection, whatever project the call was
  in. It now takes the same key as the `POST` (admin or developer; a viewer
  key is a `403`): no key is a `401`, and a `pc_id` no call of the key's
  project holds is a `404` that does not say whether it exists. Nothing
  reaches pipecat in either case. See the upgrade notes.

### Fixed

- **A Twilio number is found by its exact E.164** (#344). Looking a number up
  on a Twilio account (bind with `twilio_account_sid` +
  `twilio_auth_token`, and release) used Twilio's `PhoneNumber` filter,
  which matches partially, and took the first result: an account holding a
  longer number that contains the one asked for could bind, or release, that
  other number. The lookup now reads every match and takes only the one
  whose number is exactly the E.164; with none, the number is not on the
  account (`422`).
- **Telnyx not answering a bind is a `502`** (#344). A timeout or dropped
  connection while bind looked a number up on Telnyx was a `500`; it is now
  a `502` `telnyx_error`, like any other Telnyx failure on bind. Bind's other
  responses are unchanged: Telnyx refusing its lookup is still a `502`.
- **Binding with a Twilio account that rejects its credentials is a `422`**
  (#321). Looking a number up on a Twilio account (bind with
  `twilio_account_sid` + `twilio_auth_token`, and now release) met the
  Twilio SDK's bare "Unable to fetch page" exception on a refused request,
  so a wrong Auth Token was a `500` instead of the documented `422`. The
  lookup now reads Twilio's status, and runs off the event loop.
- **A viewer key is refused on every route that changes something** (#335).
  These routes accepted any key, so a viewer key could change what an agent
  retrieves from, overwrite a call's analysis or start a call:
  - `POST /v1/agents/{agent_id}/knowledge-bases` and
    `DELETE /v1/agents/{agent_id}/knowledge-bases/{kb_id}` (link and unlink a
    knowledge base);
  - `POST /v1/calls/{call_id}/analysis/rerun` (replaces the stored analysis
    and spends LLM calls);
  - `POST /v1/webrtc/connect` (starts a browser call, as `POST /v1/chat` and
    an outbound call, which already needed a writing key, start theirs).

  Each now needs an admin or developer key, like every other change; a viewer
  key gets `403 forbidden` and nothing changes. Every mutating route that
  takes an API key is now checked for this in the test suite, not only those
  under `/v1/agents` and `/v1/knowledge-bases`. A viewer key that relied on
  one of these must be replaced by a developer key.
- **Numbers in event payloads are redacted** (#313, #336 review). With
  redaction on, a JSON number of seven digits or more inside a payload (a
  tool's `arguments` or `result` on `tool.result`, the call's `metadata` on
  `call.ended`) went out raw in webhooks and on the event log, while the
  tool invocation row stored it redacted: only strings were analysed. Such a
  number is now analysed as the digits it is written with, as tool
  invocations and takeaways already were, so `{"phone": 4155550132}` reads
  `{"phone": "<PHONE_NUMBER>"}`. Numbers at a payload's top level
  (`duration_ms`) are TurnCall's own and are not analysed.
- **A Twilio call that rings out no longer reports `call.started`.** Every
  status callback but `completed` and `failed` went out as `call.started`, so
  an outbound call that rang and was never answered sent it at `ringing` and
  again at `busy` or `no-answer`. Now only `in-progress` (answered) sends it;
  `ringing` sends nothing, and `busy` and `no-answer` store a `call.ended` row
  on the call's log and send nothing, as a Telnyx hangup does. The status and
  `ended_reason` (`customer_busy`, `customer_did_not_answer`) are unchanged.
- **`voicemail.detected` takes its place on the call's log.** It was stored
  with sequence number 0, so it read back first, before the call started.
- **`/health` reports a reachable database as healthy.** It executed the
  isolation level's name as a statement, which SQLAlchemy refuses, so every
  deployment read `"database": "unhealthy"`. `/ready` was unaffected.
- **`smart_turn_detection: false` turns Smart Turn off** (#269). The flag only
  stopped TurnCall adding Smart Turn; pipecat 1.12's *default* stop strategy is
  Smart Turn V3, so the model still ran and waited after VAD's
  `silence_timeout_ms` window — the two waits stacked on exactly the agents
  that asked to avoid it. The turn now ends when VAD reports silence. An S2S
  agent on `turn_detection: "pipecat_vad"` had the same stacked wait and is
  VAD-only too, as documented. On server-side VAD the turn handling stays
  pipecat's (an OpenAI realtime service recommends its own), on the shared
  model below.

- **One Smart Turn model per process** (#271). Every call loaded its own ONNX
  session of the same Smart Turn weights, plus the inference buffers ONNX
  Runtime keeps per session. The session is now loaded once and shared; each
  call keeps its own audio buffer and speech state. Measured per call on a pod:
  ~123 MiB before, ~45 MiB after. That covers S2S on server-side VAD too, where
  pipecat builds its default Smart Turn analyzer on every call.

- **Shorter full garbage-collection pauses** (#270). From about 6 calls a
  pod, a full collection ran every couple of minutes and paused the whole
  process — every call's audio at once — for 300–800 ms, mostly scanning
  objects (pipecat and its imports) that live as long as the process. The app
  now freezes its startup heap (`gc.freeze()`) before serving, so a full
  collection scans only what calls allocate. Full collections still run: in
  three frozen load runs they paused for 43–255 ms, once 749 ms on a step where
  the host was overloaded. The longest audio gap at 6 calls was 63–82 ms
  frozen, against 90–354 ms without.

- **A campaign's claim and janitor are index lookups** (#229, #232). The claim
  ordered every pending target by position and filtered out the ones not yet
  due, so it walked each deferred target ahead of the first due one — on every
  dial, quadratic over a campaign whose early targets wait on a retry or a
  closed calling window (4,000 rows filtered to find one, measured). It is now
  two partial-index lookups: a target whose `next_attempt_at` has come — a
  retry, or one deferred to its window — is claimed first, earliest first,
  then the never-dialled ones in list order. The janitor, which reads claims
  across every campaign on every pass, has its own partial index instead of a
  table scan. Migration `a8e4c2f6d0b1`.

- **Downgrading past weighted campaigns refuses instead of deleting them**
  (#234). Revision `f7c3d5e9a1b4`'s downgrade deleted every campaign with no
  single agent — and, by cascade, its targets and their attempt history — to
  restore the `NOT NULL`. It now stops with an error naming how many exist;
  give each an `agent_id` or delete it, then downgrade again.

- **A campaign target's `variables` are bounded** (#229): at most 50 and
  4,096 characters of them, names included — a `422` past either. `targets`
  was capped at 10,000 but each target's variables were not, so one request
  could write any amount into the database and into the agent's prompt.

- **A dial the carrier placed keeps its live-call slot** (#224). Any failure
  after the carrier had placed an outbound call — writing its SID or its
  `call.started` — freed the slot while the phone rang, so another dial could
  be admitted into it and the project ran one call over its limit once the
  first call's media stream took its slot back. A slot is freed now only when
  nothing was dialled; a campaign target in that case ends `failed` with
  `dial_not_recorded` and keeps its reservation.

- **A call cancelled while its control channel subscribes closes the
  channel's Redis client** (#215). The subscribe step cleaned up only on an
  error, and cancellation is not one, so a call torn down in that window — a
  forced shutdown, a caller gone at once — left its dedicated connection open.

- **A call-init inline agent cannot mix Deepgram Flux into an STT failover
  group either** (#223, #204). The API refused such a group in both
  directions, but an inline agent from call-init never passes the API's
  schema, so it reached the build and a switch across the Flux line left every
  turn waiting on pipecat's 5s timeout. The build applies the same rule now and
  the call fails with `pipeline_error`, saying why.

- **A retried Telnyx `call.initiated` opens the call once** (#217). A retry
  arriving while the first delivery was still resolving the agent (a slow
  call-init) found no call row yet and opened the call again: a second
  live-call slot, a second row, duplicate `call.initializing`/`call.started`,
  and a second `answer` for the same leg. The two are now serialized on the
  leg's `call_control_id`.

- **A Telnyx call whose `answer` fails is hung up** (#217). The call was
  failed and its slot freed, but nothing told Telnyx to end the leg — and an
  answer that reached Telnyx with only its reply lost left the caller on a
  live, silent line until they hung up.

- **An Asterisk call whose bridge fails is hung up** (#209). The bridge was
  the one step of the ARI edge with no recovery: if ARI refused it, the caller
  and the pipeline stayed up unbridged, each hearing silence, the live-call
  slot held, until `max_call_duration_seconds`. Both channels are hung up now,
  which ends the call as any other ending does.

- **The Helm chart's Ingress no longer publishes `/metrics` and the probes**
  (#210). It routed `/`, so `/metrics` and `/ready` — no credential, each
  counting a pod's live calls — were reachable from the internet. It routes
  `ingress.paths` now (`/v1`, `/webhooks`, `/ws`); the kubelet and the
  PodMonitor reach the pod directly.

- **A request's writes are committed before its response is sent.** The
  request session committed in a yield dependency, which FastAPI runs *after*
  the response by default — so Twilio could open the media stream named in the
  inbound TwiML before the call row existed, and the caller heard silence
  (`media_stream_call_not_found`; seen in CI). Binding a number had the same
  race with the first call to it. The session is now `scope="function"`, which
  needs FastAPI 0.121 or later.

- **`POST /v1/calls/{id}/handoff` switches the live call** (#216). It used to
  move the call record to the target agent from the API process and change
  nothing in the call: the caller kept talking to the previous agent's prompt
  and tools while every event named the new one. The call now applies it
  itself, over the control channel, through the same prepare / commit / apply
  path as the model's `handoff_to_agent` (#144) — so the record moves only
  when the pipeline does, and a caller talking over it cannot leave it half
  done. Same contract as `/messages`: 200 with what the call applied (the
  agent and the `tools` it now advertises), **409 `call_not_live`**, **504
  `call_did_not_acknowledge`**, 409 with the call's reason (`openai_live`,
  which refuses the tool too; a call not in progress). A target agent outside
  the call's project is now a 404 — the endpoint and the tool used to accept
  any agent id. A handoff whose target cannot be loaded is no longer committed
  to the record either, on either path.

- **`POST /v1/calls/{id}/messages` reaches the live call** (#215). It used to
  record `context.injected` and answer 200 while the call's model never saw
  the message: nothing in the call read that event. The request now travels
  over a per-call Redis channel (`call:{id}:control`) that the task running
  the call holds for its lifetime, on every voice transport, and the endpoint
  answers only with what the call itself applied: **409 `call_not_live`** when
  nothing is running the call, **504 `call_did_not_acknowledge`** when it does
  not answer within 2s, 409 with the call's reason when it refuses (an S2S
  call, whose model reads no context added after it starts). The message is
  added to the context for the agent's next turn. The unused in-process
  `SessionManager` is gone; the channel is also what reaches a call held by
  another replica.

- **A transfer's own record could be lost with the call** (#220). Every
  carrier ends the agent's media stream as part of a transfer, which cancels
  the pipeline — and the `transfer_call` tool still recording
  `call.transferred` and the call's status with it, so the call could read as
  the caller hanging up. The tool now runs shielded, as `end_call` does.

- **Knowledge bases never reached an inline agent** (#260). Links live on an
  agent row, and a call-init `agent` or an eval `inline` target has none, so
  every mode was dropped without a word — on every builder agent with an
  attached app, since those answer call-init inline. An inline config now
  names its own in `knowledge_bases` (the link's fields), checked against the
  call's project: one from elsewhere, or missing, is skipped with a warning
  and the call still connects. A stored agent keeps its links as the one
  source, so `knowledge_bases` on agent create or update is a 422.

- **Knowledge bases did nothing on a speech-to-speech agent** (#254). Every S2S
  build skipped the agent's knowledge bases without a word. `prompt` mode's text
  now leads the instructions on every provider, and `tool` mode's
  `query_knowledge` reaches whoever calls tools: Realtime, Gemini Live and Nova
  Sonic themselves, or gpt-live-1's delegate, whose delegation paragraph names
  it. On `openai_live` a `tool`-mode KB counts as a tool, so it brings a
  `client` delegate — whose `llm` key is then checked like any other.
  `auto` mode cannot run ahead of a realtime model: it is skipped with a
  build-time warning, and the agent read carries a `knowledge_mode_unsupported`
  warning naming the mode and provider.

- **Tools on `openai_live` agents never ran** (#243). An S2S call opened its
  session with a fresh, empty context instead of the pipeline's own, and
  gpt-live-1 configures its session — its delegate's tools included — from the
  first context it sees. The delegate therefore had no tools for the whole call.
  The session now opens on the pipeline's context.

- **A call with no recorded audio kept `recording_status: in_progress`
  forever** (#189). pipecat skips its flush when nothing was buffered (a call
  whose providers refused before anyone spoke), so the status never moved and
  `call.ended` waited out its 15-second recording timeout. It now ends
  `failed`, the same as an empty flush, and `call.ended` goes out at once.

- **A greeting the caller never heard was saved to the transcript** (#188).
  The assistant transcript is recorded before the TTS, so when the TTS refused
  its key the agent's first message still appeared as spoken, in the call's
  events, the `call.ended` transcript, and scenarios built from the call. Text
  handed to a TTS that can no longer work is no longer recorded.

- **An eval that failed because a provider refused the agent said only "no
  response".** A provider error that leaves the pipeline running (Bedrock's
  model-access check, say) made the agent silent, and the iteration failed on
  `not satisfied within 60000ms: no response text yet`. Each iteration's entry
  now carries `agent_errors`, everything the agent's pipeline reported, and on
  a failed iteration they lead `failures` as `{kind: "agent_error", reason}`.

- **A call whose pipeline could not be built never sent `call.ended`.** It was
  marked `failed` with no reason recorded, and subscribers heard nothing. It
  now records `error.raised` and dispatches `call.ended`, on every transport.
  `call.ended` also no longer waits out the 15-second recording timeout for a
  call that never started.

- **`POST /v1/webrtc/connect` answered the offer even when the agent could not
  start.** pipecat's request handler logs an exception from the connection
  callback and answers anyway, so the browser connected to a peer with no
  pipeline behind it and heard silence. It now gets
  `500 pipeline_start_failed`, naming the reason and the call id, and the
  half-made connection is closed.

- **An eval run whose project credentials could not be decrypted was graded on
  the platform's keys.** It is now `errored` before any iteration runs, naming
  the reason (ADR-0020 fails closed).

- **An eval worker running older code than its files reported a bare
  `ImportError`.** A dev worker left running while its bind-mounted source
  changes mixes old and new modules. The run's error now says to restart the
  eval worker.

- **`llm.api_key` was silently discarded on the `openai` provider** (#161). It
  now wins over the platform key, as it already did for `anthropic`,
  `openrouter` and `custom_openai`.

- **Deleted documents and knowledge bases left their files in object storage
  forever** (#168). Both deletes now remove the stored files after the commit,
  best-effort: a failure is logged, never raised. Documents also get a
  per-upload storage key, so re-uploading a filename to one knowledge base no
  longer overwrites the first document's file. Project deletion already swept
  storage, but missed recordings stored by Twilio's recording callback
  (`recordings/{call_id}/{sid}.wav`); it now sweeps both recording shapes.
  The `ambience_storage_orphan` and `project_delete_storage_orphan` log events
  are now one event, `storage_orphan`. Update any alert on the old names.

### Documentation

- **SIP trunk guides** (#322, ADR-0025). TurnCall has no SIP endpoint of its
  own, and the Phone Numbers guide now says so and links two guides for the
  ways a SIP trunk reaches an agent. *SIP trunk through Asterisk*: the
  carrier's side (the dialled number in the request, `+E.164` or `E.164` but
  not national unless mapped in the dialplan; continuous audio), the trunk's
  context and `Stasis` dialplan, ARI and `websocket_client.conf`, then the
  `asterisk` credential and binding. *SIP trunk through Twilio or Telnyx*: a
  number bound in TurnCall, dialled from the phone system over a Twilio
  Elastic SIP Trunk or a Telnyx SIP Connection, with each carrier's
  termination setup and its warning that a Twilio number still on a trunk
  ignores the voice URL binding sets, and that binding a Telnyx number takes
  it off its SIP Connection.
- **A compliance mapping** (#318, ADR-0024): the Compliance guide maps
  redaction, caller numbers off spans, retention, recordings, the audit log,
  self-hosting, encrypted project credentials and signed webhooks to GDPR
  articles, then to HIPAA Security and Privacy Rule sections. It says plainly
  that TurnCall holds no certification and is not legal advice, states the
  controls' limits (the call record kept raw and never deleted by retention;
  signing, call-init and MCP secrets stored unencrypted, #338; Twilio and
  WhatsApp webhooks unverified without their token or app secret, #337, #339;
  `http://` URLs accepted, #340), and lists what stays the operator's: DPAs
  and BAAs with the STT, LLM, TTS and carrier vendors, infrastructure and
  encryption at rest of call data, access control, backups, the carrier's own
  recording copy, the call record, text conversations (not covered by
  retention) and audio (not redacted).
- **What redaction does to webhooks.** Server Events gains a Redaction
  section: every payload is redacted before it is sent, so a subscriber never
  sees a redacted value, except in the `call.ended` transcript of a call that
  was live when redaction was turned on; text at any depth, and long numbers
  below the top level, become `<ENTITY_TYPE>`, number fields `<PHONE_NUMBER>` (the Redaction
  guide's new [Number fields](docs/guides/redaction.mdx#number-fields) section
  says which and when), identifiers are never analysed, and text is
  `<REDACTED>` while the detector is unavailable. The call record keeps the
  numbers. Signature verification now advises refusing a timestamp more than
  five minutes old.
- **Recording cannot be turned off.** The Redaction guide said to turn
  recording off for audio that holds PII; there is no such setting, as every
  voice call is recorded. It now says to keep recordings briefly with
  `recordings_days`.
- **The person behind a change, in the Compliance guide.** Its audit-log and
  unique-user-identification rows now say that the TurnCall builder records
  the person behind each change made in its console, in an append-only log of
  its own linked to the entries here (builder-api #101, #102). A change made
  straight against the API is still attributed to its key only.

### Upgrade notes

- **Set `TWILIO_AUTH_TOKEN` and `WHATSAPP_APP_SECRET`, and `TURNCALL_ENV`,
  before upgrading any deployment that is not a development one** (#337,
  #339). A Twilio or WhatsApp webhook nothing can verify is now refused with a
  `403` everywhere but `TURNCALL_ENV=development`: without the token, every
  inbound Twilio call gets Twilio's application error and every text is
  dropped; without the secret, every WhatsApp event is. A Twilio number on
  its own account is verified by its own token and needs nothing.
  `TURNCALL_ENV` still defaults to `development`, which accepts them
  unverified, so a production or staging deployment that never set it must
  set it now (startup says so when it is unset).
- **Send the API key on the ICE trickle too** (#342). A client that sends
  `PATCH /v1/webrtc/connect` without `Authorization` now gets a `401`, and
  its call loses the candidates it trickles. pipecat's browser transport
  already sends the `POST`'s headers on every `PATCH`, so a client that set
  `Authorization` in `webrtcRequestParams.headers` (as
  `examples/webrtc-client` does) needs nothing. A proxy that forwards the
  `PATCH` must add the key as it does for the `POST`.
- **Viewer keys lose four routes** (#335): linking and unlinking an agent's
  knowledge base, `POST /v1/calls/{call_id}/analysis/rerun` and
  `POST /v1/webrtc/connect` now need an admin or developer key. Replace a
  viewer key used for any of them with a developer key.
- **Spans no longer carry caller numbers.** `PIPECAT_TRACE_INCLUDE_PII` defaults
  to `false`, so `turncall.from_number` / `turncall.to_number` are gone from a
  call's spans. If you search traces by caller number, set
  `PIPECAT_TRACE_INCLUDE_PII=true` to keep the previous behaviour.

## [2.0.0]

Evals — automated behavioural testing for agents — plus tool calling on text,
pronunciations, and the two things that made this a major release: six removed
endpoints and one config that is now refused at create.

### Added

- **Evals: automated behavioural testing for agents.** State what an agent must
  do and have TurnCall check it on every change. A **scenario** is one saved
  test: **scripted** (`turns:`, a fixed conversation with per-turn expectations)
  or a **simulation** (`persona:`, an LLM plays the caller and a judge rules on
  the outcome). A **run** is one scenario x target x modality over N iterations.
  The engine is `pipecat.evals`; only the **transport** is swapped, so an eval
  exercises the real STT/LLM/TTS construction, the real VAD and smart-turn
  wiring, the real tool bridge and KB retrieval — which is where #63, #64, #65
  and #67 all lived. See `adr/0018` and the [Evals
  guide](https://docs.turncall.com/guides/evals).
  - `POST/GET/PUT/DELETE /v1/eval-scenarios[/{id}]` and `/v1/eval-runs[/{id}]`,
    plus `GET /v1/eval-runs/batches/{batch_id}` for one verdict without
    transcripts. Both lists page in the house `paginated` envelope, and
    `?view=summary` answers a single run's "is it done yet" without its payload.
  - **A run is executed by a dedicated worker**, `turncall-eval-worker` — same
    image, own entrypoint, fed by a Redis list. Never the API process: eval load
    in that event loop is dead air on a live call (ADR-0004).
  - **Three target forms.** `{"type": "agent", "agent_id"}` pins a version,
    `{"type": "agent_name"}` resolves whatever is published at run time, and
    `{"type": "inline", "agent": {...}}` runs a config with no row at all,
    validated by the same schema an agent create uses and snapshotted verbatim.
  - **Tools are mocked, and fail closed.** A scenario's `tool_mocks` short-circuit
    dispatch before the branch that would run it — webhook, MCP and built-in
    alike. Under the default `mock_only` a tool with no mock is refused and the
    iteration is `errored` naming it; `live` is the typed opt-in for read-only
    tools. Mocks belong to the scenario, never the run: "the booking succeeds"
    and "the booking fails" are two tests.
  - **Text or audio.** `modality: audio` synthesizes the caller's turns into the
    agent's real STT and transcribes the agent's real TTS for the judge, so a
    results entry carries both views of each reply — `content` (what the judge
    read) and `text` (the agent's own words) when they differ. Text is the
    default, and is a fraction of the time.
  - **Choose the model that judges.** `judge: {provider, model, temperature,
    endpoint}` and `simulator: {...}` on a scenario; `provider` is a closed set
    mapping to factories TurnCall ships, because pipecat's own escape hatch is a
    dotted import path and that is remote code execution from a request body. A
    definition naming one anywhere pipecat reads it is refused at the boundary,
    and `api_key` is not a field — the key is the platform's. `EVAL_JUDGE_*` and
    `EVAL_SIMULATOR_*` set one for the whole platform; precedence is
    narrowest-wins and a block is taken **whole**, never merged field by field.
    A `judge` on a **run** request is a 422: a run is what it was queued as.
  - **`jev` as a judge** — TypeSafe's classifier, a few hundred milliseconds per
    verdict with a *calibrated* probability. Judge-only, and structurally so: the
    allowlist is split by role, so `simulator: {"provider": "jev"}` fails naming
    the role. Needs `TYPESAFE_API_KEY` and the `evals-jev` extra, neither checked
    at scenario create — both are properties of a deployment, and a stored
    scenario outlives the one it was written against, so the **run** fails naming
    which is missing.
  - **The explainer is TurnCall's choice.** Since pipecat 1.12 a verdict comes
    from a classifier and a second model writes the reason behind every `no`. For
    a classifier-only judge pipecat falls back to its own default LLM service — a
    local Ollama — so a judge pointed at a hosted vendor would silently acquire a
    dependency on a service nobody provisioned. TurnCall compiles the explainer
    from the platform's configured judge, and turns reasons **off** explicitly
    where the platform configured none. LLM-backed judges are left exactly alone.
  - **A confidence beside every verdict** a judge produced — a scripted
    expectation and its failures, a simulation's goal verdict, each per-turn
    verdict of a judged metric. `null` is not zero: zero is "certain of the
    opposite". Pipecat 1.12 carries it and then drops it building the results
    TurnCall reads, so the field reads `null` today and starts reporting on the
    bump that threads it through, with no change here.
  - **`eval.run.started` / `eval.run.completed` webhooks.** The completed event is
    comprehensive — status, counts, every iteration's transcript, failures and
    `tool_calls`, and all three snapshots — so a subscriber never has to call
    back, and an **errored** run dispatches it too. The envelope gains a nullable
    `eval_run_id` beside `call_id` and `session_id`; existing subscribers are
    unaffected.
  - **`turncall eval` CLI**, whose exit code can gate a pull request: `0` only
    when every run passed, `1` a failure, `2` an error, `3` a cancellation, `4` a
    batch still running when the command stopped waiting, `64` a usage mistake.
    Distinct because "your agent regressed" and "we could not check" want
    different alerts, and a judge outage must never read as a regression. A
    scenario file is the API request body unchanged — one schema, one validator —
    so `POST /v1/eval-runs` also takes an inline `scenario`.
  - **Derive a scenario from a real conversation.** `POST
    /v1/eval-scenarios/from-call` and `/from-session` convert a finished call or
    a text session into a draft: caller utterances become turns, replies become
    `text_contains` expectations to sharpen, and every recorded tool invocation
    becomes both a `function_call` expectation carrying the arguments actually
    used *and* a mock seeded with what that tool really returned. `save: false`
    (the default) returns it for review, and the response says out loud that it
    is a draft.
  - **A run carries its own `warnings`** — a mock that can never fire, an agent
    whose real tools are allowed to run, a scenario in which nothing can fail, a
    judge that changed since the last verdict, a worker older than the code it
    runs. They ride on the run, `eval.run.completed`, the batch summary and
    `turncall eval show`, because the author reads the run and never the worker's
    log.
  - **Three snapshots per run** — `resolved_config`, `resolved_scenario`,
    `harness_config` — the last recording which judge answered, which voice and
    STT produced the audio, and **which code ran it**: `worker_version`,
    `worker_started_at`, and `worker_stale` when the package's newest source is
    younger than the process that imported it. That case is otherwise silent: the
    run succeeds and the verdict looks ordinary while the feature the scenario
    depends on was simply not in the process.
  - **`POST /v1/webrtc/connect` returns `call_id`** beside `sdp`/`type`, so a
    browser that just held a conversation can fetch its transcript or convert it
    into a scenario. Additive.
  - New settings: `EVAL_MAX_CONCURRENT_RUNS`, `EVAL_MAX_RUN_DURATION_SECONDS`,
    `EVAL_MAX_QUEUED_SECONDS`, `EVAL_JANITOR_INTERVAL_SECONDS`,
    `EVAL_MAX_ITERATIONS`, `EVAL_MAX_SCENARIOS_PER_REQUEST`, `EVAL_TTS_CACHE_DIR`,
    `EVAL_JUDGE_PROVIDER` / `_MODEL` / `_TEMPERATURE`, `EVAL_SIMULATOR_*`,
    `TYPESAFE_API_KEY`.
- **`tts.pronunciations`** — a flat word → IPA map, the outbound mirror of
  `stt.keyterms`. One spelling, each provider's own markup generated from it:
  Cartesia inline phonemes, ElevenLabs SSML `<phoneme>`, Deepgram's inline
  object; OpenAI has no pronunciation surface. Validation is **TurnCall's own**,
  because pipecat's IPA utilities normalize and tokenize but never reject — so
  `met-FOR-min` would be accepted, inserted into the markup and do nothing
  audible, and is a 422 naming the word. `/…/` and `[…]` are stripped and the
  value stored canonically. On ElevenLabs the flag that makes it work,
  `enable_ssml_parsing`, is a constructor argument no `tts.extra` could reach;
  the pipeline factory passes it for exactly the agents that have pronunciations
  *and* a model pipecat lists as phoneme-capable, never for everyone, since SSML
  parsing changes how all of that agent's text is read. On a voice that cannot
  read the markup the markup is **withheld**: a `<phoneme>` tag reaching a
  service that is not parsing SSML is spoken aloud (measured at 3.5–4.3x the
  audio), which is worse than the mispronunciation. One unusable word is spoken
  as written; it never silences the reply.
- **`warnings` on an agent read.** Two things an author needs told that cannot be
  refusals, and were reaching nobody: a "pronunciation" equal to its own
  spelling (`{"Metformin": "metformin"}` — legal IPA *notation*, so the 422
  cannot see it, and it reaches the voice as real markup, measured at a quarter
  more audio than saying nothing), and a voice that will not read the markup,
  which was known at pipeline build and said only to a log, mid-call, about a
  config written days earlier. Derived on every read and never stored, so an
  agent whose model moved out from under its pronunciations starts saying so
  without being edited.
- **`stt.keyterms`** — one list of vocabulary hints (product names, SKUs,
  surnames), mapped to each provider's own spelling: `keyterm` on Cartesia,
  `keywords` on OpenAI, `keyterms` on ElevenLabs, and on Deepgram whichever the
  model takes — `keyterm` for `nova-3*`/`flux*`, `keywords` for everything
  older. Deepgram answers the wrong one with `INVALID_QUERY_PARAMETER` rather
  than ignoring it, so a hand-written `extra: {"keyterm": ...}` on a Nova-2
  agent killed the call at connect; `keyterms` resolves per model and beats the
  same key in `extra`. Cartesia honours them on `ink-2*`/`ink-preview*` only and
  caps a connection at 100 terms / 1200 characters. Blanks and duplicates are
  dropped at the API boundary.
- **`user_idle_timeout_ms`** (default `10000`, `0` disables) ends a call the
  caller has walked away from — the line stays open otherwise, billing, until
  the max-duration cap. After the agent stops speaking, that much silence plays
  `idle_message` ("Are you still there?"); a second consecutive silence ends the
  call with a new `ended_reason` of **`customer_silent`**. Speaking again resets
  the count. Cascade speaks the line through TTS; S2S has no TTS stage, so the
  model is asked to check in and words it itself. Like `max_duration_reached` it
  records a marker event (`call.customer_silent`) and is inferred **above**
  `assistant_ended` — ending the call is how the guard gives up, so without its
  own branch it read as `assistant_ended_call`. Not to be confused with
  `silence_timeout_ms`, the VAD stop window inside a turn.
- **Tool calling on SMS, chat and WhatsApp text.** Text conversations could not
  call tools at all — not MCP, not the agent's own webhook tools. An agent that
  books meetings on a phone call would claim it had booked one over SMS, or
  apologise; either way the webhook was never hit. Every provider family has its
  own dialect: OpenAI-compatible (`tools`/`tool_calls`), Anthropic
  (`input_schema` + tool_use/tool_result blocks) and Bedrock Converse
  (`toolSpec`/`toolUse`/`toolResult`). Calls within one round run concurrently.
  The loop caps at five rounds and then re-asks with the tools withheld, so a
  model that keeps calling them still answers in words. Built-ins stay voice-only
  — all four resolve against a live `call_id`.
- **Text-session tool calls are recorded.** `tool_invocations.call_id` is
  nullable, `session_id` joins it, and a CHECK enforces exactly one. Read them
  back at `GET /v1/chat/sessions/{id}/tool-invocations`. `output_json` and
  `latency_ms` were columns nothing wrote; both paths now time the call and
  classify the result through one helper, so a tool that returned
  `{"error": ...}` is recorded as failed on voice as it already was on text.
- **`execution_mode: "async"` does something.** Accepted by the API schema since
  tools existed and read nowhere. `async` lets a slow tool outlive an
  interruption and delivers its result when it arrives; `sync` stays the default,
  which matters most for the built-ins — `end_call` must not hang up after the
  caller said never mind. Voice only, and now documented as such.
- **`max_call_duration_seconds` caps a call.** Validated 60–14400 and enforced
  never, so a stuck call ran until the carrier stopped it, billed the whole way.
  A watchdog on the session cancels the worker, and the call finalizes the way a
  hangup does — with its own `ended_reason`, `max_duration_reached`, rather than
  reading back as `customer_ended_call`.
- **`interruption_enabled: false` turns off barge-in.** Cascade only. On S2S the
  realtime service owns turn-taking, so it is warned about rather than
  half-applied.
- **`silence_timeout_ms` sets the VAD stop window**, and
  `PIPECAT_VAD_CONFIDENCE_THRESHOLD` is honoured. Both were documented and inert.
- **`MCP_MAX_TOOLS_TOTAL`** (default 100) — `MCP_MAX_TOOLS_PER_SERVER` is per
  server and does not compose; ten servers at the default would advertise 500
  tools. **`MCP_CONNECT_TIMEOUT_SECONDS`** (default 10) bounds discovery: past it
  the call proceeds with no MCP tools rather than not proceeding, because losing
  the tools degrades the agent and losing the call is an outage.
  **`TOOL_MAX_RESPONSE_BYTES`** caps a custom webhook tool's result, as
  `MCP_MAX_RESPONSE_BYTES` already did for MCP.
- **A `live` test marker** for the paths a hermetic suite cannot reach, excluded
  from `make test` and skipped per missing credential. It exists because the
  `openai` 3 upgrade moved the HTTP stack under every OpenAI service, and the
  `mcp` 2 rename emptied every MCP server, without a test going red either time.

### Changed

- **Pipecat 1.12**, up from 1.9 via 1.10 and 1.11, and the `openai` 3 SDK. See
  `adr/0019`. What 1.12 forces:
  - **`mcp>=2.1.1,<3` — mcp 1.x is unreachable**, because pipecat's `mcp` extra
    requires 2.x. The dual-spelling reads that briefly supported both lines
    (`Tool.inputSchema` / `CallToolResult.isError`) are gone: under that pin they
    can never match, and on 2.x the old names survive only as pydantic aliases.
  - **`transformers>=5.10`**, verified rather than assumed —
    `LocalSmartTurnAnalyzerV3` loads a bundled ONNX model, not a transformers
    one, and discriminates correctly under 5.12.1 in ~45ms.
  - **`nltk` is replaced by `sentencex`**; startup stops paying the NLTK warm-up.
  - **Voicemail detection is one processor over an explicit `LLMClassifier`.** A
    custom prompt *replaces* the classifier's defaults, and those defaults are
    what ask for the JSON object it parses — so passing one straight through
    would have left every verdict unparseable, visible only as a log line and a
    call that never detects voicemail.
  - **The `ollama` judge is an `LLMClassifier` with a 60s budget.** 1.12 judges a
    simulation one bot turn per call and fires those calls together against a 10s
    default; a local Ollama serializes them, measured at 18s each for four
    concurrent against 4.5–5.8s alone. Every turn timed out and the run reported
    `judge call failed`, which from outside is indistinguishable from an agent
    regression.
  - **An empty user turn is handled rather than dropped**, and on by default. A
    turn VAD opened and closed with no transcript — a cough, a door, an
    unrecognized word — gets a single LLM run if it *interrupted* the agent, so
    the agent asks the caller to repeat instead of stopping dead mid-sentence;
    one arriving while the agent was already waiting is left alone, because
    replying to a passing truck is intrusive. A turn *outcome*, not a fourth
    timeout.
- **`stt.model` and `llm.model` empty now mean "the provider's own default"**,
  resolved per provider the way `_tts_model_voice` already did for TTS. Both
  were provider-agnostic fields holding one provider's model: `stt.model`
  defaulted to Deepgram's `nova-3-general`, which the other three answer 400 to,
  and `llm.model` to OpenAI's `gpt-4o-mini`, which Anthropic answers
  `404 not_found_error` to. `model_dump()` persisted those defaults into every
  existing agent's `config_blob`, so the sentinel value is read as unset rather
  than migrated. Unset resolves to `nova-3-general` / `gpt-transcribe` /
  `scribe_v1` / `ink-whisper` for STT, and `gpt-4o-mini` (openai) /
  `claude-sonnet-5` (anthropic) for LLM. An explicitly chosen value passes
  through untouched, so a wrong one is reported by the provider rather than
  silently replaced (ADR-0016's rule).
  - **BREAKING:** `ollama`, `custom_openai`, `bedrock` and `openrouter` have no
    house model — the model *is* the deployment there — so an agent naming none
    is rejected with `422` at create rather than failing mid-call.
- **BREAKING: `/v1/test-suites` and `/v1/test-runs` are gone.** Six endpoints
  accepted rows into two tables, and the background worker their docstring
  promised was never written; nothing outside the router and the models ever
  read either. `eval_scenarios` / `eval_runs` replace the namespace. The
  migration is one-way: its downgrade raises rather than recreating tables whose
  rows described runs that never happened.
- **Claude is sent no `temperature`**, on the direct Anthropic path and through
  Bedrock. Current models answer `400 "temperature is deprecated for this
  model."`, which made the service unusable and ended the call on its first LLM
  turn. The deprecation belongs to the model, so it travels with the model rather
  than the endpoint — the other vendors Bedrock fronts still get one. Set one
  deliberately on an older model with `llm.extra`.
- **MCP name precedence follows config order.** Discovery moved into
  `asyncio.gather` for one round trip instead of three, and name claiming went
  with it — so "first server wins" had become "fastest server wins", and which
  server owned a contested name could differ call to call with no config change.
  Fetch and claim are now separate: the handshakes still overlap, the claim is a
  serial pass in the agent's configured order.
- **Each TTS provider composes its own text-transform list.** Internal, no
  behaviour change: pronunciation markup is provider-specific where markdown
  stripping was not, and the pronunciation transform is registered **last**,
  because a later transform would eat the markup it produces.
- **`ruff format` runs in CI** and the configured line length is 88, which is
  what the code was already wrapped at. `line-length = 100` never matched, so
  anyone with format-on-save got a diff full of lines they never touched. The 79
  reformatted files were verified AST-identical, not merely test-passing.

### Fixed

- **The caller waited seconds in dead air, 2.4–5.7s per turn.** VAD and Smart
  Turn were charged one after the other rather than together:
  `BaseSmartTurn.append_audio` is handed `is_speech=vad_user_speaking`, so its
  silence counter could not start until VAD had waited out its own window — at
  the 800ms default, end-of-turn 1.8s after the caller stopped talking before
  the LLM was even called. With Smart Turn on, the model decides the turn and VAD
  uses pipecat's 0.2s.
- **Every eval run scored `errored` — no eval feature had ever reported a
  verdict.** Pipecat's harness is an RTVI client and the server half is an
  `RTVIProcessor` in the bot's pipeline plus an `RTVIObserver` on the task;
  TurnCall built neither, so every iteration died at the `bot-ready` handshake.
  Because `errored` is kept out of every rate, the outage read as "no signal".
  Both halves are wired now, eval path only, and gated on `is_eval` so no live
  call gains a processor.
- **A handoff could not survive the caller talking over it.** `handoff_to_agent`
  queues the target's system prompt and tool schema as frames, and pipecat drains
  interruptible frames on barge-in — so a caller saying "okay, thanks" mid-handoff
  could lose either one, leaving the model running the previous agent's prompt,
  advertising the previous agent's tools, over a context already wiped, with the
  new agent's handlers underneath. Nothing raised and nothing was logged. Both
  frames are queued `interruptible = False`.
- **The handoff switch is atomic against cancellation.** The drain is only half of
  what an interruption does; the other half cancels in-flight sync function calls,
  and no flag saves a frame the handler never built. A DB round-trip *after* the
  commit left the call record handed off while the live pipeline kept the previous
  agent, logged nowhere, since `CancelledError` slips past `except Exception`. The
  handoff is now prepare-then-apply: all I/O and both frames are built before the
  commit, and the apply has nothing left to wait on and nothing left that can
  raise.
- **`tts.pronunciations` could never work on ElevenLabs.** Pipecat reads SSML
  `<phoneme>` tags only with a phoneme-capable model *and* `enable_ssml_parsing`,
  a constructor kwarg — so an author's `tts.extra` landed in `Settings.extra` and
  never reached it. The warning was correct with no configuration path out of it.
- **Cancelling a running eval run now stops it, and stays cancelled.** `DELETE`
  set the column and achieved nothing else: nothing in the iteration loop re-read
  the row, so every remaining iteration ran and was paid for, and `finish_run`
  then wrote `passed` over `cancelled` and announced `eval.run.completed` for a
  conversation the user had asked to stop.
- **A hung eval iteration is bounded.** `harness.run()` had no budget while every
  service it drives is a network call, so a provider that accepts a connection and
  never answers left `execute_run` never returning — four hangs produced a worker
  that consumed no queue, executed nothing, logged no error and still answered as
  healthy. One iteration is abandoned as `errored` after
  `max(EVAL_MAX_RUN_DURATION_SECONDS/iterations, 180s)`.
- **The janitor's cutoff is derived from the run's iterations.** It swept any
  `running` row older than a flat 900s, and there is no heartbeat — so a worker
  that was alive and working looked identical to one that had crashed. Runs
  legitimately exceed it (iterations are serial and the guide recommends
  several), the row flipped to `errored` mid-flight, and the CLI took the first
  terminal status it saw: a slow run reading as a regression, the one thing the
  design exists to avoid.
- **A tag's fan-out is capped.** `POST /v1/eval-runs` capped `iterations` and
  nothing else, and `list_scenarios` had no limit, so one POST with a popular tag
  and `iterations: 50` queued `scenarios x iterations` full conversations — a
  project with 200 scenarios on `pre-publish` is 10,000 conversations from one
  HTTP call. `EVAL_MAX_SCENARIOS_PER_REQUEST` (50) refuses it naming the count,
  never truncating: a batch that ran 50 of 200 reports a verdict for a suite that
  never ran.
- **The CLI's deadline fits the batch.** Its default was a per-run budget used as
  a whole-batch one, so ten scenarios behind a four-slot worker — three waves —
  made the command give up on a healthy suite and report an outage. The default
  is now per run multiplied by how many were queued, and a reached deadline says
  so on stderr naming the runs still going.
- **A batch that outran the deadline exits 4, not 2.** "We stopped waiting" and
  "nobody could tell" were one code, and they have different remedies: a timed-out
  run is still being executed and will reach a real verdict minutes later, and the
  fix is `--timeout` or more worker slots, not the judge.
- **A scenario that cannot fail is warned about.** Validation round-trips through
  pipecat's parser, which checks shape and not strength, so a scenario whose every
  expectation is a content-free event parses, stores, runs and reports `passed`
  forever against an agent whose LLM returns nothing — after a provider 404
  pipecat still emits an empty `llm_response`. `scenario_cannot_fail` and
  `content_free_expectations` ride on the create/update response and the run.
  Such a run scores **`failed`**, never `errored`.
- **The eval worker's readiness probe stopped looking like a failure.** Every
  iteration logged `opening handshake failed` with a traceback and nothing was
  wrong — pipecat's client checks the bot is listening by opening a TCP connection
  and closing it without sending anything. The cost was not the noise: a probe
  succeeding and a bot that could not be reached produced the same traceback.
- **A sentence the STT split in two became two scenario turns** in `from-call`.
  Consecutive caller utterances are merged, because one sentence split by the STT
  is one turn.
- **Audio evals work in Docker**, and `from-call` reads the caller's role
  correctly. Pipecat's TTS cache default lives under `$HOME`, which a container
  loses on recreate — every repeat run would re-synthesize every caller turn — so
  `EVAL_TTS_CACHE_DIR` exists and is mounted.
- **MCP servers now connect on WebRTC and WhatsApp voice.** `mcp_servers` was
  read only by the Twilio path, so an agent with MCP tools got none of them on
  those transports — the model saw an empty tool list.
- **S2S advertised none of its MCP tools.** It connected the servers, paid the
  handshake and held the sessions for the whole call; only the schema was
  missing.
- **A tool name claimed by two MCP servers** put two identical function names in
  one request, which providers reject, and the loser was advertised but
  unreachable. Precedence is built-in > agent tools > MCP, the loser skipped and
  logged. An MCP server can no longer claim a built-in's name — a discovered tool
  called `end_call` was advertised and then hung the call up.
- **The tool that ran was not always the tool the model was shown.** The
  advertised schema gave a contested name to the agent's own tool; voice dispatch
  asked the MCP manager first. The model saw the customer's description and
  parameter schema, and the MCP server's tool ran.
- **`handoff_to_agent` moved the prompt and left the tools.** The model believed
  it was the new agent while holding the previous one's. It now replaces the
  advertised set; MCP servers are not re-connected mid-call, which is logged.
- **An unbounded MCP tool result** put megabytes into the context, where it is
  charged on every later turn until the call dies on context length. Over the cap
  the model gets an error plus a 512-byte preview.
- **Discovered tool schemas lost their `$defs`.** A property holding a `$ref`
  arrived pointing at nothing, and providers reject an unresolvable `$ref` — so
  the tool failed at the model rather than here.
- **`{"error": null}` was recorded as a failure**, so an endpoint that always
  includes the field had every successful call logged as failed.
- **A cancelled pipeline build leaked its open MCP sessions** — cleanup sat after
  the `try` and an `except Exception` walked past it.
- **`llm.extra` reaches Anthropic.** The documented way to set a temperature
  crashed on the first LLM turn: the SDK dropped `temperature`, `top_k` and
  `top_p` from `messages.create()`. Any key the SDK will not take by name now
  travels in `extra_body`, checked against the SDK's own signature.
- **A default `google` S2S agent sent OpenAI's model to Gemini.** `s2s.model`
  and `s2s.voice` default to OpenAI Realtime's values whatever the provider, so
  a `provider: google` agent naming neither arrived holding `gpt-realtime-2.1`
  and `alloy`. Gemini closed the socket with 1008 (`models/gpt-realtime-2.1 is
  not found for bidiGenerateContent`) and the call died at connect. Both
  sentinels are now swapped for Gemini's own — `gemini-3.8-live` and `Charon` —
  the way the `aws` and `openai_live` paths already did.
- **The model reported in `call.ended` is the one that actually ran.** Post-call
  analysis resolved its default independently of the pipeline, so the two could
  name different models for the same call.
- **An inline agent from call-init crashed every Twilio call**, and then got no
  post-call processing on any transport — and since that trigger dispatches
  `call.ended`, such a call completed and told nobody. WebRTC additionally
  resolved its inline config and discarded it, so every browser call with an
  inline agent finalized silently. All three transports now share one sentinel
  and one rule about recording the config. See `adr/0017`.
- **`AgentConfig.knowledge_bases` and `ToolDefinition.is_builtin` are gone.**
  Neither could ever be set or read. A `config_blob` written while the field
  existed still loads.

### Security

- **An agent's secrets leaked through an eval run.** The config masker guarded
  exactly one exit — the agent response builder — and the eval feature added two
  more paths carrying the same `config_blob`: `GET /v1/eval-runs/{id}`, where a
  viewer key read every credential in the clear, and `eval.run.completed`, which
  POSTed them to a webhook subscriber. An inline target leaked the same way. The
  masker moved to `domain/config_secrets.py` so no exit can miss it, and a run's
  `resolved_config` is stored already masked — nothing reads it back to execute
  anything, so the secret has no reason to sit in a JSONB column with its own
  retention story.
- **A judge or simulator cannot name a `factory`.** Pipecat's escape hatch is a
  dotted path handed to `importlib.import_module`: fine for a file on disk,
  remote code execution from a request body. One naming a factory anywhere pipecat
  reads it — `judge.eval`, `judge.eval.explainer`, `judge.transcription`,
  `simulator`, `user.speech` — is refused at the API boundary, and `api_key` is
  not a field, so a credential can never be stored in JSONB and masked forever
  after.
- **The BYOM/MCP URL allowlist could be talked past.** `fnmatch`'s `*` spans
  `/`, so a pattern an operator would reasonably write —
  `https://*.trusted.com/*` — also accepted `https://evil.com/x.trusted.com/y`,
  `https://evil.com/?u=https://api.trusted.com/` and a `user@` prefix. Each
  leaves for `evil.com` from inside the network, which is the exact thing the
  gate exists to stop. A pattern naming a host must now match the URL's host on
  its own. Patterns naming no host (`*`, `*openai*`) are unchanged.
- **MCP urls now go through the same SSRF gate** as `llm.base_url` and
  `s2s.base_url`. They had none.

### Documentation

- **`adr/0017` — calls without an agent row.** Why the zero-UUID sentinel
  exists, why it is never written to `active_agent_id`, and what a null
  `agent_id` on an event does and does not mean.
- **`adr/0018` — the eval transport bridge and worker.** **`adr/0019` — pipecat
  1.12 and its classifiers.** `evals-design.md` carries the scenario vocabulary,
  every pipecat 1.11 field reachable through a stored `definition`, and the five
  that parse and do nothing here.
- **The API Reference was missing every eval endpoint** — all twelve routes, plus
  `DELETE /v1/projects/{id}`, the authenticated key route and the text-session
  tool invocations. The reference lists its pages by hand, so a shipped endpoint
  is published only if someone adds one, and the docs read as complete
  throughout: a missing page looks exactly like a feature nobody wrote.
  `make check-docs-nav` now diffs the navigation against `docs/openapi.json` in
  both directions and fails CI either way.
- **A config-reachability guard** (`tests/unit/`) walks the agent-config models
  and asserts each field is read by something outside `domain/` and `api/`. The
  most common defect here is a field that validates, stores, round-trips through
  the API and does nothing; eight had landed in two days. A new field must now be
  consumed or explicitly declared inert.
- **The pipecat parser surface scenario validation stands on is pinned.**
  Round-tripping a stored `definition` uses two of pipecat's *private* functions,
  because 1.11's public loaders take a path and a stored scenario has no file — so
  a bump that moves them would take out every create, update and queued run at
  once. `tests/unit/test_pipecat_parser_contract.py` makes that a red build naming
  the version instead.
- `voicemail_expected_duration_seconds` remains inert, with the reason recorded:
  Pipecat's `VoicemailDetector` has nothing that means "expected greeting
  length", and mapping it onto `voicemail_response_delay` would put a 15-second
  pause after the greeting. It needs behaviour or removal on the next major.

### Upgrade notes

- **Run `alembic upgrade head`.** Six migrations: the eval tables and their
  later columns, `tool_invocations.session_id`, and the drop of the dead
  `test_suites` / `test_runs` stub. That drop is **one-way** by design.
- **Why major.** Two breaking changes, both listed above: `/v1/test-suites` and
  `/v1/test-runs` are removed, and an agent on `ollama`, `custom_openai`,
  `bedrock` or `openrouter` that names no `llm.model` is now a `422` at create.
  Such an agent never worked — it failed on its first LLM turn — but a create
  that returned `201` now returns `422`, so anything provisioning agents should
  name the model explicitly before upgrading.
- **`mcp` 1.x is no longer supported.** Pipecat 1.12's `mcp` extra requires
  `>=2.1.1`, so a deployment pinning 1.x must move.
- **Existing agents gain an idle hangup.** `user_idle_timeout_ms` defaults to
  `10000`, so a caller who goes quiet for ten seconds after the agent stops
  speaking now hears `idle_message`, and a second silence ends the call with
  `ended_reason: customer_silent`. Set it to `0` to keep the previous behaviour.
- **An empty user turn now gets a reply** where it interrupted the agent. A
  cough or a door that barges in mid-sentence produces one LLM run — the agent
  asks the caller to repeat — rather than the agent stopping dead.
- **Evals need a second process.** `turncall-eval-worker` (same image, own
  entrypoint) and Redis; without it a run is accepted and never executed. The
  default judge and persona are a **local Ollama**, so a simulation or an `eval:`
  assertion needs one reachable even when the agent's own LLM is hosted — or set
  `EVAL_JUDGE_PROVIDER` / `EVAL_SIMULATOR_PROVIDER`.
- **`eval_run_id` is new in the webhook envelope**, and null on every
  non-eval event. Additive.

## [1.1.0]

### Added

- **AWS Bedrock as an LLM provider and Amazon Nova Sonic 2 as S2S.** Model ids
  pass through verbatim, so direct ids, cross-region inference profiles and
  provisioned-throughput ARNs all work. Credentials come from a per-agent `aws`
  block — an assumed `role_arn`, static keys, a named profile, or the ambient
  chain. See `adr/0016`.
- **`openai_live` S2S provider** — OpenAI's `gpt-live-1`, which is full duplex:
  it listens and speaks at once and handles being talked over itself, where the
  Realtime API is turn-based. Reasoning and tool calls can be delegated to a
  backend text model with `s2s.extra.backend_model`. `s2s.temperature` is
  accepted here and `s2s.turn_detection` must stay `server_vad`.
- **`stt.extra` and `tts.extra` now reach the provider.** Both fields existed
  and were silently ignored. On Deepgram, `stt.extra` unlocks
  `profanity_filter`, `diarize`, `redact`, `keyterm`, `version` and
  `utterance_end_ms`.
- **`tts.speed` now works on Deepgram, OpenAI and ElevenLabs**, not Cartesia
  alone. `1.0` still sends nothing.

### Changed

- **Upgraded to Pipecat 1.9.0** from 1.8.1.
- **Provider defaults refreshed.** These apply only to agents that do *not* set
  the value explicitly:

  | Role | Was | Now |
  |---|---|---|
  | Deepgram STT | profanity filter on | off |
  | OpenAI STT | `gpt-4o-transcribe` | `gpt-transcribe` |
  | Cartesia TTS | `sonic-3.5` | `sonic-3.6` |
  | HeyGen avatar | VP8 | H264 |

  `gpt-4o-transcribe` is withdrawn on 2027-02-26 and LiveAvatar has deprecated
  VP8. **Deepgram transcripts are no longer profanity-filtered**: the filter
  rewrites matched words rather than tagging them, so one false positive
  silently altered a transcript you store, analyse and receive in `call.ended`.
  Set `stt.extra.profanity_filter` to `true` to restore it.
- **Retrieved knowledge is attached as `developer`, not a second `system`
  message.** It is guidance for one turn, not the agent's instructions, and
  outside OpenAI a mid-conversation `system` message was downgraded to `user`
  anyway.
- **The system prompt is set on the LLM service rather than inserted as the
  first context message.** Internal; no config or API difference. The
  conversation context now starts empty, so a transcript no longer carries the
  instructions as a phantom first turn.

### Fixed

- **A Deepgram voice name was being sent to the other TTS providers.**
  `tts.model` and `tts.voice` both defaulted to `aura-2-helena-en` whatever the
  provider, so an agent choosing Cartesia, OpenAI or ElevenLabs without naming
  a model and voice was misconfigured. Each provider now falls back to its own.
- **`end_call` during voicemail detection.** While the classifier's gate was
  closed, the frame ending a call could be dropped instead of reaching the
  pipeline, leaving the caller on the line until the idle timeout.
- **Assistant transcripts no longer contain text the model never wrote.** A
  word-timing event matching nothing left to speak was added to the
  conversation as spoken text.
- Replaced end-of-life Bedrock model ids and documented inference profiles.
- Stopped the localstack compose overriding real AWS credentials.

### Upgrade notes

- **Config that was silently ignored now takes effect.** If an agent carries a
  stray `stt.extra`, `tts.extra` or `tts.speed`, it did nothing before this
  release and does something now. Worth a scan before upgrading.
- No REST API, agent config schema or webhook payload shape changed, which is
  why this is a minor release. The `call.ended` transcript *content* changes
  with the profanity filter default.

## [1.0.0]

First public release.

### Changed

- **Upgraded to Pipecat 1.8.1** from 1.5.0.
- **Provider model defaults refreshed.** These apply to agents that do *not*
  set a model explicitly. Pin the old value in your agent config to keep it:

  | Role | Was | Now |
  |---|---|---|
  | ElevenLabs TTS | `eleven_turbo_v2_5` (deprecated upstream) | `eleven_flash_v2_5` |
  | Cartesia TTS | `sonic-3` | `sonic-3.5` |
  | Deepgram STT | `nova-2` | `nova-3-general` |
  | OpenAI Realtime | `gpt-4o-realtime-preview` | `gpt-realtime-2.1` |
  | Realtime transcription | `gpt-4o-transcribe` | `gpt-realtime-whisper` |

- **A pipeline now ends when a service can no longer work.** Pipecat 1.8 stops
  using an STT/TTS/LLM after an unrecoverable failure (rejected API key, unknown
  model or voice, a connection that won't re-establish). TurnCall sets
  `processor_unusable_policy=END` so the call terminates and finalizes promptly,
  rather than leaving the caller on a silent line until the idle timeout.

### Fixed

- **Per-agent Deepgram STT config was ignored.** `stt.model` and `stt.language`
  were hardcoded to `nova-2` and `en` for the Deepgram provider, discarding
  whatever an agent specified. Both are now honored. If you have non-English
  agents on Deepgram, their configured language now actually takes effect —
  verify transcription quality after upgrading.

### Security

- Widened `.gitignore` env patterns to `.env*` and `*.env`. The previous rules
  (`.env`, `.env.*`) did not match variants such as `.env2`.

### Removed

- `pyyaml-include` (GPL-3.0) is no longer in the dependency tree; Pipecat
  dropped it in 1.6.0. TurnCall's dependencies are now fully permissive.

### Added

- CI: lint, test (with pgvector + migrations), and `bandit` on every push and PR.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and this changelog.
- `webrtc-video` extra, required for video avatars over SmallWebRTC once
  Pipecat 2.0 drops OpenCV from the `webrtc` extra.
