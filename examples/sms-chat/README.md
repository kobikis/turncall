# SMS Chat Example

A customer support agent that handles both **phone calls** and **SMS text messages**
on the same Twilio number.

## What it does

- **SMS**: Customers text the number and get AI-powered replies
- **Voice**: Same number also handles voice calls (via Pipecat pipeline)
- **Session memory**: SMS conversations persist for 24 hours
- **Chat API**: Same agent reachable via `POST /v1/chat` for web/API integration

## Prerequisites

1. **Twilio account** with a 10DLC-approved phone number (required for SMS)
2. **OpenAI API key**
3. **Docker** (for Postgres + Redis)
4. **ngrok** (to expose local server to Twilio)

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   TWILIO_ACCOUNT_SID=ACxxxxxxxx
#   TWILIO_AUTH_TOKEN=xxxxxxxx
#   OPENAI_API_KEY=sk-xxxxxxxx
```

### 2. Start infrastructure

```bash
make docker-up    # postgres + redis
make migrate      # create database tables (includes SMS tables)
make run          # start server on http://localhost:8090
```

### 3. Expose with ngrok (new terminal)

```bash
ngrok http 8090
# Copy the https://xxxx.ngrok.io URL
```

### 4. Run setup script

```bash
python examples/sms-chat/setup.py \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  --server-url "https://xxxx.ngrok.io"
```

This creates the project, agent, and configures your Twilio number for both voice and SMS.

### 5. Text your number!

Send a text message to your Twilio number and get a reply from the AI agent.

| You text | What happens |
|----------|-------------|
| "Hi, what are your hours?" | AI replies with business hours |
| "I need to reschedule" | AI asks for details, continues conversation |
| (wait 24+ hours, text again) | New session starts with fresh context |

### 6. Use the Chat API

```bash
# Send a chat message (same agent, API channel)
curl -X POST http://localhost:8090/v1/chat \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "AGENT_ID",
    "message": "What are your hours?"
  }'

# Continue the conversation using session_id
curl -X POST http://localhost:8090/v1/chat \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "AGENT_ID",
    "session_id": "SESSION_ID_FROM_PREVIOUS",
    "message": "Can I come in Saturday?"
  }'

# List sessions
curl http://localhost:8090/v1/chat/sessions \
  -H "Authorization: Bearer YOUR_API_KEY"

# View messages in a session
curl http://localhost:8090/v1/chat/sessions/SESSION_ID/messages \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Architecture

### SMS Flow
```
Customer texts +1234567890
  → Twilio POSTs to /webhooks/twilio/sms/inbound
  → Resolves phone number → Loads agent
  → Creates or resumes 24h session
  → Builds LLM message history from session
  → Calls OpenAI chat completion
  → Returns TwiML <Response><Message>reply</Message></Response>
```

### Chat API Flow
```
Developer POSTs to /v1/chat
  → Auth via Bearer token → Resolves agent
  → Creates or resumes session (by session_id or previous_chat_id)
  → Same LLM flow as SMS
  → Returns {success: true, data: {session_id, reply, ...}}
```

### Session Management
- Sessions auto-create on first message
- Resume within 24 hours (same customer + number pair for SMS)
- Expire after 24h inactivity → new session on next message
- Background task cleans up expired sessions every 15 minutes

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID`, `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
