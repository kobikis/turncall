# 0009 — Call transfer: cold + warm with caller message and operator briefing

TurnCall already cold-transfers a Twilio call (`<Dial>target</Dial>` via
`calls.update(twiml=…)`) and exposes both an LLM `transfer_call` tool and a
manual `POST /v1/calls/{id}/transfer` endpoint. But two advertised params are
**non-functional stubs**: `transfer_mode` (`warm`/`cold` execute identically) and
`pre_transfer_message` (accepted, never played — `announce_url` is hardcoded
`None`). There is no operator briefing.

This ADR makes warm transfer real and adds a caller-facing message and an
operator briefing, with a small, TurnCall-native surface.

## Decision

### API surface (native fields, not a mode enum)

`transfer_mode` stays the behavioral switch; two optional message fields drive the
rest. Same shape for the LLM tool and the REST endpoint:

```
transfer_call(
  target_number: str,              # E.164, required
  transfer_mode: "cold" | "warm",  # default "cold"
  transfer_message?: str,          # spoken to the CALLER before the dial
  briefing?: str | {from_summary: true},  # spoken to the OPERATOR (warm only)
  fallback_message?: str,          # spoken to the caller if the operator doesn't answer
  reason?: str,
)
```

- `transfer_message` **renames the non-functional `pre_transfer_message`** (a no-op
  today, so the rename is safe) and is now actually played.
- `briefing` is `warm`-only; ignored on `cold`. It is either a literal string or
  `{from_summary: true}` to auto-generate a summary from the transcript.

### Cold = blind, Warm = whisper

**Cold** — optionally say the caller message, then dial; the AI leg drops:

```xml
<Response>
  <Say>{transfer_message}</Say>           <!-- if set -->
  <Dial action="/webhooks/twilio/transfer-result/{call_id}">
    <Number>+1operator</Number>
  </Dial>
</Response>
```

**Warm** — same, but the operator hears the briefing (Twilio *whisper*) after they
answer and before the caller is bridged. The caller hears ringing, not the
briefing:

```xml
<Number url="/webhooks/twilio/whisper/{call_id}">+1operator</Number>
```
```xml
<!-- whisper endpoint returns: -->
<Response><Say>{briefing or generated summary}</Say></Response>
```

Whisper on `<Number url>` is chosen over a conference bridge: Twilio-native, no
second leg / hold-music / conference lifecycle, one extra webhook.

### Voicing: Twilio `<Say>` (v1)

Both the caller message and the briefing are rendered with Twilio's built-in TTS
(`<Say>`), not the agent's TTS provider. Zero audio-synthesis/hosting pipeline.
Accepted cost: the caller hears a different voice for the transfer line than the
agent. Upgrade path: synthesize with the agent TTS and `<Play>` a hosted URL (the
TwiML builder already has the `<Play url>` slot).

### Failure: graceful fallback, no AI resume

Both flows dial with `<Dial action="/transfer-result/{call_id}">`. If
`DialCallStatus` is not `completed` (busy / no-answer / failed), the result
endpoint plays `fallback_message` (default "Sorry, we couldn't connect you.") and
hangs up. `ended_reason` stays **`transferred`** (a transfer was *attempted* — the
`call.transferred` event outranks the failure checks in ADR-0008's precedence);
distinguishing attempted-vs-connected is a later refinement, not v1.

Resuming the AI on a failed transfer is **out of scope**: swapping the TwiML
closes the media-stream WebSocket and tears down the pipeline, so "agent comes
back" would require building a fresh session — deferred.

### Voicemail on the operator line: AMD notify, caller may leave a message

The `fallback_message` path above only fires on busy/no-answer/failed. When the
operator's line rolls to **voicemail**, Twilio reports the leg as `completed` (the
machine "answered"), so the caller **is bridged** and can hear the greeting and
leave a message. v1 accepts that outcome and adds a *signal*, not a block:

```xml
<Number url="/whisper/{call_id}"
        machineDetection="Enable"
        amdStatusCallback="/webhooks/twilio/transfer-amd/{call_id}"
        amdStatusCallbackMethod="POST">+1operator</Number>
```

Twilio runs AMD in parallel and POSTs the result; the dial/bridge is **not gated**
on it (`machineDetection="Enable"`, not `DetectMessageEnd`), so no added bridge
latency. The `transfer-amd` endpoint reads `AnsweredBy` (`human` /
`machine_start` / `fax` / `unknown`) and emits a **`transfer.answered`** webhook
event with `{target_number, answered_by}` so the backend knows whether the
transfer reached a human or voicemail.

Because AMD resolves *after* the leg answers, the result arrives after the bridge
— so this **notifies, it does not prevent**. Truly stopping the caller from
reaching voicemail needs a decision *before* bridging (a separate outbound leg
with `machine_detection`, bridge only on `human`) — the conference model, which is
the deferred upgrade.

Known wart on **warm + voicemail**: the briefing whisper plays *into* the
voicemail before the caller is bridged (it gets recorded ahead of the caller's
message). Mitigation: steer warm transfers to staffed numbers; a personal mobile
likely to hit voicemail is a poor warm-transfer target.

### Transient transfer state in Redis

When a transfer is issued, the intent (`target`, `mode`, `transfer_message`,
`briefing`/`from_summary`, `fallback_message`) is written to Redis under
`transfer:{call_id}` with a short TTL (~300s). The whisper and transfer-result
endpoints read it when Twilio calls back. It is control-plane scratch data, not a
durable fact about the call — Redis (already a dependency), not the call row.

For `briefing.from_summary`, the whisper endpoint gathers the transcript and runs
the agent's LLM to produce the summary **on fetch** (not at transfer time), so the
operator hears the latest context.

### Manual / out-of-band control = the existing REST endpoint

`POST /v1/calls/{id}/transfer` (same params) is the control API for manual and
backend-driven transfers. A *dynamic* transfer (LLM signals intent → TurnCall
asks your `server_url` to resolve the destination mid-call) is **out of scope for
v1**: anything it enables, a backend can already do by calling the REST endpoint
with its own routing logic, and it would add a synchronous hot-path webhook + new
contract. Clean to add later.

## Scope boundaries (v1)

- **Twilio PSTN only.** Whisper/`<Dial>` are Twilio TwiML; WebRTC and WhatsApp
  transfer are out of scope.
- **`number` destinations only** (E.164). No SIP.
- **Per-transfer params** (tool args / REST body); no agent-level transfer config.
- **Recording stops at transfer** — the media stream closes when the TwiML is
  swapped. Not continued onto the operator leg.
- **Voicemail is detected, not prevented** (AMD notify). A caller can still reach
  the operator's voicemail and leave a message; blocking it is the deferred
  conference upgrade.
- New `/whisper`, `/transfer-result`, and `/transfer-amd` webhooks validate
  `X-Twilio-Signature` like the existing Twilio handlers.

## Consequences

- `transfer_mode` and `transfer_message` become functional; `briefing` /
  `fallback_message` are new.
- Three new Twilio webhook endpoints (`whisper`, `transfer-result`,
  `transfer-amd`), one Redis key per in-flight transfer, an LLM summary call only
  when `briefing.from_summary` is used, and AMD billed per transfer leg.
- New event `transfer.answered` (`{target_number, answered_by}`) reports
  human-vs-voicemail to subscribers.
- `ended_reason = transferred` whenever a transfer was attempted (derived from the
  `call.transferred` event, ADR-0008), including the no-answer fallback path.
- Requires `PUBLIC_BASE_URL` for warm transfer and the fallback message (the
  callbacks have no inbound request to derive the host from); cold + caller
  message work without it.
