# 0007 — `agent_id` and `event_id` on webhook events

Every webhook TurnCall delivers carries an envelope (`event`, `project_id`,
`call_id`, `session_id`, `agent_id`, `event_id`, `timestamp`) plus a per-event
`payload` (see `webhook_delivery.WebhookEvent` / `deliver_webhook`). Two gaps:

- **No `agent_id`** anywhere — a consumer receiving `transcript.final` or
  `call.ended` could not tell which agent produced it without a second API call.
- **`event_id` and `session_id` were always `null`** on call events. The
  envelope had the fields; nothing populated them.

## Decision

### `agent_id` lives in the **envelope**, resolved centrally in `dispatch_event`

`agent_id` is a top-level envelope field (a sibling of `call_id`/`session_id`),
not a payload key. It is resolved in `dispatch_event`: a caller may pass it
explicitly (sms/chat), otherwise for call events it is looked up from
`call.active_agent_id`:

```python
resolved = agent_id
if resolved is None and call_id is not None:
    call = await get_call_by_id(session, call_id)
    resolved = call.active_agent_id if call else None
event.agent_id = str(resolved) if resolved else None
```

- **Envelope, not payload** — identity/routing fields belong in the envelope
  alongside `call_id`; the payload stays purely event-type-specific.
- **`dispatch_event`, not `emit_call_event`** — the richest events (`call.ended`
  from `call_analysis_trigger`, `transcript.final` from `observability`,
  `tool.result` from `tool_bridge`) call `dispatch_event` directly and bypass
  `emit_call_event`. The choke point that covers all call-scoped events is
  `dispatch_event`.
- **Resolved at dispatch time**, so a [[handoff]] is reflected — you get the
  *current* `active_agent_id`, not one captured when the processor started.
- **Always present, `null` when unresolved** (e.g. `call.initializing` before
  routing resolves an agent). Consistent shape beats present-or-absent.
- **Lookup only after the subscriber check.** `dispatch_event` already returns
  early when no subscription matches; the `get_call_by_id` lookup happens only
  when someone is actually listening, so silent calls pay nothing.
- **sms/chat events** (`call_id is None`) pass `agent_id=session_row.agent_id`
  explicitly, so no call lookup happens.

### `event_id` is a fresh UUID per logical event

`event_id = str(uuid4())`, generated **once** in `dispatch_event` before the
delivery fan-out — *not* the `CallEventRow.id`, and *not* regenerated per HTTP
attempt.

- **Stable across the retry loop.** `deliver_webhook` retries up to
  `MAX_RETRIES = 5`; all retries of one logical event carry the same `event_id`,
  so a consumer can dedupe redeliveries (the at-least-once case where it
  processed the event but the response timed out).
- **Shared across subscribers.** One `WebhookEvent` fans out to all matching
  subscribers with the same `event_id`.
- **Why not `CallEventRow.id`?** Decoupling the wire identity from the DB row
  keeps non-call events (sms/chat, which have no `CallEventRow`) on the same
  scheme, and avoids threading the row id back out of `create_call_event`.

### `session_id` left as-is

Already populated for sms/chat events (`_dispatch_session_event` /
`_dispatch_chat_event` pass `session_id`). For call events it is legitimately
`null` — there is no chat session — so no change.

## Consequences

- The webhook payload shape is a consumer-facing contract; this adds keys, never
  removes, so it is backward-compatible for existing consumers.
- `dispatch_event` does one extra `get_call_by_id` per delivered call event.
  Bounded by "has subscribers", PK-indexed, accepted.

## Not done

- `chat.created` still omits `agent_id` (its dispatcher has the message row, not
  the session row). Add if a consumer needs it.
- `analysis.completed` (`CallEventType.ANALYSIS_COMPLETED`) is defined but never
  dispatched — analysis ships inside `call.ended`. Unrelated pre-existing gap.
