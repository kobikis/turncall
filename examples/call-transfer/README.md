# Call Transfer Example

Transfer a live call to a human — cold (blind) or warm (with an operator
briefing), with an optional message to the caller. Covers the LLM-initiated path
(the agent decides) and the manual/out-of-band path (your backend decides).

> Design reference: [`adr/0009-call-transfer-warm-cold.md`](../../adr/0009-call-transfer-warm-cold.md).
> Twilio PSTN only.

## Quick Start

```bash
# 1. Start turncall: make run
# 2. Create project + a transfer-savvy agent + bind your number:
python examples/call-transfer/setup.py \
  --twilio-number-sid PN_YOUR_SID \
  --twilio-number +15551234567 \
  --transfer-to +15557654321        # the human/operator number

# Call your Twilio number and ask for "a human" / "billing" —
# the agent calls the transfer_call tool. Or drive it manually (below).
```

## Transfer modes

| `transfer_mode` | `transfer_message` | `briefing` | Caller hears | Operator hears |
|-----------------|--------------------|------------|--------------|----------------|
| `cold` | – | – | (bridged immediately) | – |
| `cold` | ✓ | – | "Connecting you…" then bridged | – |
| `warm` | – | string | ringing, then bridged | the briefing |
| `warm` | ✓ | `{from_summary:true}` | message, then ringing, then bridged | an LLM summary of the call |

- **`transfer_message`** — spoken to the **caller** before the dial (both modes).
- **`briefing`** — spoken to the **operator** before the caller is bridged (warm
  only). Either a literal string, or `{"from_summary": true}` to auto-generate a
  summary from the transcript.
- **`fallback_message`** — spoken to the caller if the operator is busy / doesn't
  answer, then the call ends (`ended_reason=telephony_failed`).

## Manual transfer (REST control API)

Transfer any in-progress call out-of-band. `CALL_ID` comes from the
`call.started` event or `GET /v1/calls`.

```bash
# Cold, no message
curl -X POST http://localhost:8090/v1/calls/CALL_ID/transfer \
  -H "Authorization: Bearer tc_YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"target_number": "+15557654321", "transfer_mode": "cold"}'

# Cold, with a caller message
curl -X POST http://localhost:8090/v1/calls/CALL_ID/transfer \
  -H "Authorization: Bearer tc_YOUR_KEY" -H "Content-Type: application/json" \
  -d '{
    "target_number": "+15557654321",
    "transfer_mode": "cold",
    "transfer_message": "One moment, connecting you to a specialist.",
    "fallback_message": "Sorry, no one is available right now."
  }'

# Warm, static briefing to the operator
curl -X POST http://localhost:8090/v1/calls/CALL_ID/transfer \
  -H "Authorization: Bearer tc_YOUR_KEY" -H "Content-Type: application/json" \
  -d '{
    "target_number": "+15557654321",
    "transfer_mode": "warm",
    "transfer_message": "Please hold while I bring in a colleague.",
    "briefing": "Caller is a premium customer disputing a double charge on order #4471."
  }'

# Warm, auto-summary briefing (LLM summarizes the transcript)
curl -X POST http://localhost:8090/v1/calls/CALL_ID/transfer \
  -H "Authorization: Bearer tc_YOUR_KEY" -H "Content-Type: application/json" \
  -d '{
    "target_number": "+15557654321",
    "transfer_mode": "warm",
    "briefing": {"from_summary": true}
  }'
```

## LLM-initiated transfer

The built-in `transfer_call` tool is available to the agent automatically. Give
it guidance in the system prompt (the setup script does this) and the model fills
in the same fields:

```
System: "If the caller asks for a human or wants billing, call transfer_call with
         target_number +15557654321, transfer_mode 'warm', and a one-line briefing
         summarizing why they're being transferred."
```

## What to watch (webhook events)

| Event | When | Key payload |
|-------|------|-------------|
| `call.transferred` | transfer initiated | `target_number`, `transfer_mode`, `reason` |
| `transfer.answered` | operator leg answered | `target_number`, `answered_by` (`human`/`machine`) |
| `call.ended` | call finished | `ended_reason`: `transferred` (success) or `telephony_failed` (no answer) |

Subscribe with the [events-webhook](../events-webhook/) example to see them live.

## Voicemail behavior (read this)

If the operator's line rolls to **voicemail**, Twilio treats it as answered, so
the caller **is connected and can leave a message** — v1 does not block this. You
get a `transfer.answered` event with `answered_by="machine"` so your backend
knows. On **warm**, the briefing is spoken *into* the voicemail before the caller
is bridged, so prefer **staffed numbers** for warm transfers. Preventing voicemail
entirely is a future (conference-based) upgrade. See ADR-0009.

## Quick run

```bash
./run.sh --transfer-to +1555…
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
