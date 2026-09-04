# Receptionist Example

A dental clinic receptionist that answers phone calls, understands intent,
and routes callers to the right destination.

## What it does

- **Appointments** → transfers to a human at the front desk
- **Billing questions** → hands off to a billing AI agent
- **General questions** → answers directly (hours, address, doctors)
- **Emergencies** → tells them to call 911

## Prerequisites

1. **Twilio account** with a phone number
2. **OpenAI API key**
3. **Cartesia API key** (for Sonic TTS + Ink STT — get at play.cartesia.ai)
4. **Docker** (for Postgres + Redis)
5. **ngrok** (to expose local server to Twilio)

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   TWILIO_ACCOUNT_SID=ACxxxxxxxx
#   TWILIO_AUTH_TOKEN=xxxxxxxx
#   OPENAI_API_KEY=sk-xxxxxxxx
#   CARTESIA_API_KEY=sk_car_xxxxxxxx
```

### 2. Start infrastructure

```bash
make docker-up    # postgres + redis
make dev          # install dependencies
```

### 3. Run database migrations

```bash
make migrate
```

### 4. Start the server

```bash
make run
# Server starts on http://localhost:8000
```

### 5. Expose with ngrok (new terminal)

```bash
ngrok http 8000
# Copy the https://xxxx.ngrok.io URL
```

### 6. Run setup script

```bash
python examples/receptionist/setup.py \
  --twilio-number "+15559876543" \
  --twilio-number-sid "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  --server-url "https://xxxx.ngrok.io"
```

This creates the project, agents, and configures your Twilio number.

### 7. Call your number!

Try these scenarios:

| You say | What happens |
|---------|-------------|
| "I need to schedule an appointment" | Transfers to front desk |
| "I have a billing question" | Hands off to billing AI |
| "What are your office hours?" | Answers: Mon-Fri 9am-5pm |
| "I'm having a dental emergency" | Tells you to call 911 |

## Architecture

```
Phone Call → Twilio → POST /webhooks/twilio/voice/inbound
  → Resolves phone number → Loads receptionist agent
  → Returns TwiML with <Stream> → WebSocket opens
  → Pipecat pipeline: Audio → STT (Cartesia Ink) → LLM (OpenAI) → TTS (Cartesia Sonic) → Audio
  → LLM decides: answer / transfer_call / handoff_to_agent
```

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID`, `PUBLIC_BASE_URL` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
