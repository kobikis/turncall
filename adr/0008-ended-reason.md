# 0008 — `ended_reason` on `call.ended`

`call.ended` carries `status` (the coarse outcome: `completed` / `failed` /
`no_answer` / `busy`), but not *why* the call ended. A consumer can't tell a
customer hangup from an agent-initiated `end_call`, or a pipeline crash from a
Twilio failure. A granular end-reason is the field integrators lean on most for
outcome classification — `ended_reason` provides it.

## Decision

### Derived, not stored

`ended_reason` is **computed at `call.ended` build time** in
`call_analysis_trigger._run_post_call` — no new column, no migration. The signals
needed already exist by the time the comprehensive `call.ended` fires:

- `call.status` (terminal status on the call row)
- the set of event types recorded for the call
- whether an `end_call`-tool `call.ended` event (payload `source == "control"`)
  was logged

A stored column was considered and rejected: the reason adds no queryable value
the existing `status` column doesn't already give coarsely, and deriving avoids
threading a reason through ~8 termination paths.

### Snake_case vocabulary

Values follow TurnCall's existing enum style (`no_answer`, `in_progress`) rather
than kebab-case — internally consistent over matching any external convention.
Canonical set (`domain.enums.EndedReason`):

| value | meaning |
|---|---|
| `voicemail` | a `voicemail.detected` event was recorded |
| `transferred` | a `call.transferred` event was recorded |
| `assistant_ended_call` | the `end_call` tool fired (a `call.ended` event with `source == "control"`) |
| `customer_did_not_answer` | `status == no_answer` |
| `customer_busy` | `status == busy` |
| `pipeline_error` | `status == failed` with no Twilio `call.failed` event (unhandled pipeline exception) |
| `telephony_failed` | `status == failed` with a Twilio failure event |
| `customer_ended_call` | `status == completed`, no other signal (customer hung up / call finished) |
| `unknown` | nothing matched |

Precedence is top-to-bottom; first match wins (e.g. a voicemail call that then
completes resolves to `voicemail`, not `customer_ended_call`).

### Scope: only reasons a real path produces

`silence_timed_out` and `max_duration_exceeded` are **omitted** — TurnCall has no
termination path that produces them, so the enum value would
be dead. Add them if/when those termination paths exist.

## Consequences

- Pure function `infer_ended_reason` in `domain.call_state` — no I/O, fully unit
  tested.
- `_run_post_call` does one extra `distinct(event_type)` query (chosen over
  `list_call_events`, whose `limit=200` would truncate the *late* terminal
  signals on long calls) plus a small `call.ended` lookup for the control flag.
- `pipeline_error` vs `telephony_failed` is inferred from event presence/absence
  — the one fragile split. If it proves unreliable, collapse both to a single
  `call_failed` without changing the field's contract.
