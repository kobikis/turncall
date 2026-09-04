# Tools Showcase Example

Demonstrates **all tool types** available in TurnCall: built-in tools, custom webhook tools,
dynamic agent resolution (call-init), and knowledge context injection.

## What it does

Creates a **support agent** with:

1. **`end_call`** (built-in) — Ends the call when conversation is complete
2. **`transfer_call`** (built-in) — Transfers to a live agent's phone number
3. **`handoff_to_agent`** (built-in) — Switches to a billing specialist mid-call
4. **`send_dtmf`** (built-in) — Sends keypad tones (for IVR navigation)
5. **`lookup_customer`** (webhook) — Queries a mock CRM by phone number
6. **`check_order_status`** (webhook) — Looks up order details
7. **`create_ticket`** (webhook) — Creates a support ticket

Plus a **webhook server** that:
- Handles `call-init` (pre-call init) to dynamically configure the agent
- Handles tool webhook calls (lookup_customer, check_order_status, create_ticket)
- Returns `knowledge_context` and `variables` based on the caller

## Architecture

```
Phone Call → Twilio → TurnCall → call-init → Your webhook server
  → Returns: agent_id + variables + knowledge_context
  → TurnCall applies variables/context → starts pipeline

During call:
  LLM invokes tool → TurnCall POSTs to webhook_url → Your server returns result
```

## Prerequisites

1. **TurnCall server running** (`make docker-up && make run`)
2. **Twilio account** with a phone number
3. **ngrok** for exposing both TurnCall and the tool webhook server

## Quick Start

### 1. Start the webhook server (tool handler + call-init)

```bash
cd examples/tools-showcase
pip install fastapi uvicorn
uvicorn webhook_server:app --port 9000
```

### 2. Expose with ngrok (two tunnels)

```bash
# Terminal 1: TurnCall
ngrok http 8090

# Terminal 2: Webhook server
ngrok http 9000
```

### 3. Run the setup script

```bash
python examples/tools-showcase/setup.py \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxx" \
  --turncall-url "https://xxxx.ngrok.io" \
  --webhook-url "https://yyyy.ngrok.io"
```

### 4. Call your number!

Try:
- "Can you look up my account?" → triggers `lookup_customer`
- "What's the status of my order?" → triggers `check_order_status`
- "I need to file a complaint" → triggers `create_ticket`
- "Transfer me to a person" → triggers `transfer_call`
- "I have a billing question" → triggers `handoff_to_agent`
- "Send tone 1234" → triggers `send_dtmf`
- "That's all, goodbye" → triggers `end_call`

## Files

| File | Purpose |
|------|---------|
| `setup.py` | Creates project, agents, binds phone number with webhook routing |
| `webhook_server.py` | FastAPI app handling call-init + tool webhooks |
| `README.md` | This file |

## How call-init works

When a call comes in, TurnCall POSTs to your `server_url`:

```json
{
  "message": {
    "type": "call-init",
    "phoneNumber": {"number": "+15559876543"},
    "customer": {"number": "+15551112222"},
    "call": {"id": "uuid", "provider_call_id": "CA...", "type": "inboundPhoneCall"}
  }
}
```

Your server responds with the agent + runtime context:

```json
{
  "agent_id": "support-agent-uuid",
  "variables": {"customer_name": "Jane Doe", "account_id": "ACC-12345"},
  "metadata": {"crm_id": "C-789", "segment": "enterprise"},
  "dynamic_data": {
    "knowledge_context": "Customer Jane Doe (ACC-12345) is an enterprise client. She has 2 open tickets: #101 (billing dispute) and #102 (feature request). Last interaction was 3 days ago."
  }
}
```

The `knowledge_context` is prepended to the system prompt, so the agent knows who's calling
and their history before saying a word.

## How tool webhooks work

When the LLM decides to call a custom tool, TurnCall POSTs to the tool's `webhook_url`:

```json
{
  "tool_name": "lookup_customer",
  "arguments": {"phone_number": "+15551112222"},
  "call_id": "uuid",
  "project_id": "uuid"
}
```

Your server returns the result as JSON (becomes the tool response the LLM sees):

```json
{
  "customer_name": "Jane Doe",
  "account_id": "ACC-12345",
  "tier": "enterprise",
  "balance": "$0.00",
  "open_tickets": 2
}
```

## Quick run

```bash
./run.sh --webhook-url https://…
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID`, `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
