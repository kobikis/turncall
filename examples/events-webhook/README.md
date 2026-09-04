# Events Webhook Example

Receive and log all TurnCall events in real-time.

## Quick Start

```bash
# Terminal 1: Start the webhook server
cd examples/events-webhook
uvicorn webhook_server:app --port 9001

# Terminal 2: Full setup (project + agent + phone + webhook subscription)
python setup.py \
  --twilio-number-sid PN_YOUR_SID \
  --twilio-number +15551234567

# Or use an existing API key (skip project/agent/phone creation)
python setup.py --api-key tc_YOUR_KEY

# Make a call — events appear in Terminal 1
```

## Subscribe to Specific Events

```bash
python setup.py --api-key tc_YOUR_KEY --events call.ended call.started call.failed

python setup.py --api-key tc_YOUR_KEY --events transcript.final

python setup.py --api-key tc_YOUR_KEY --events "*"
```

## Available Events

| Event | When | Key Payload Fields |
|-------|------|--------------------|
| `call.initializing` | Call comes in, before pipeline | from_number, to_number, direction, transport |
| `call.started` | Pipeline starts | agent_id, agent_name, from_number, to_number |
| `transcript.final` | Each utterance (customer + agent) | text, user_id |
| `tool.called` | LLM invokes a tool | tool_name, arguments |
| `tool.result` | Tool execution finished | tool_name, status, latency_ms |
| `recording.ready` | Recording stored to local/S3 | recording_url, recording_duration |
| `call.ended` | Post-call processing complete | transcript, recording_url, summary, analysis, from_number, to_number, duration_ms |
| `call.failed` | Call errored | error details |
| `call.transferred` | Transfer initiated | target_number, transfer_mode |
| `call.agent_handoff` | Handed off to another agent | source_agent_id, target_agent_id |
| `context.injected` | Context message injected | message, role |
| `dtmf.sent` | DTMF tones sent | digits |
| `session.created` | New SMS/chat session | session_id, channel |
| `session.updated` | Session activity | message_count |
| `session.deleted` | Session expired | session_id |
| `chat.created` | New chat message | content, role |
| `error.raised` | Runtime error | error message |

## Example Output

```
======================================================================
  EVENT: call.initializing
  Call:  ea272b27-404e-4426-b03c-1a8aefb87998
  Time:  13:47:58
======================================================================
  Payload: {"from_number": "+15551234567", "to_number": "+15559876543",
            "direction": "inbound", "transport": "twilio"}

======================================================================
  EVENT: call.started
  Call:  ea272b27-404e-4426-b03c-1a8aefb87998
  Time:  13:47:58
======================================================================
  Agent: 83701ab2-c62d-4b4f-acaa-547eb5dfe761

======================================================================
  EVENT: transcript.final
  Call:  ea272b27-404e-4426-b03c-1a8aefb87998
  Time:  13:48:05
======================================================================
  Customer: Hi. Good morning.

======================================================================
  EVENT: transcript.final
  Call:  ea272b27-404e-4426-b03c-1a8aefb87998
  Time:  13:48:06
======================================================================
  Agent: Good morning! How can I assist you today?

======================================================================
  EVENT: call.ended
  Call:  ea272b27-404e-4426-b03c-1a8aefb87998
  Time:  13:48:49
======================================================================

  --- Transcript (6 turns) ---
  Customer: Hi. Good morning.
  Agent: Good morning! How can I assist you today?
  Customer: I have a few questions.
  Agent: Of course! Please feel free to ask your questions.
  Customer: Okay. I need to go over.
  Agent: Sure! Please let me know what you need to go over.

  Summary: The call was initiated by the customer who had a few questions...
  Success: fail — The call did not progress to address specific issues.
  Sentiment: neutral (satisfaction: neutral)
  Model: gpt-4o-mini (3711ms)
```

## call.ended Payload

The comprehensive `call.ended` event includes everything:

```json
{
  "event": "call.ended",
  "call_id": "ea272b27-...",
  "payload": {
    "from_number": "+15551234567",
    "to_number": "+15559876543",
    "direction": "inbound",
    "duration_ms": 51000,
    "started_at": "2026-04-17T13:47:58+00:00",
    "ended_at": "2026-04-17T13:48:49+00:00",
    "transcript": [
      {"role": "customer", "text": "Hi. Good morning.", "timestamp": "..."},
      {"role": "agent", "text": "Good morning! How can I assist you?", "timestamp": "..."}
    ],
    "recording_url": "./storage/recordings/{call_id}/{recording_sid}.wav",
    "summary": "The call was initiated by the customer...",
    "analysis": {
      "summary": "...",
      "success_evaluation": {"score": "fail", "reason": "..."},
      "sentiment": {"overall": "neutral", "customer_satisfaction": "neutral"},
      "model": "gpt-4o-mini",
      "duration_ms": 3711
    }
  }
}
```

## Debug Endpoint

```bash
curl http://localhost:9001/events?limit=10
```

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
