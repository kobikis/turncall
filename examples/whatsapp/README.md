# WhatsApp Example

A customer support agent that handles **voice calls** and **text messages**
over WhatsApp Business using the WhatsApp Cloud API and Pipecat.

## What it does

- **WhatsApp Text**: Customers message your WhatsApp Business number and get AI-powered replies
- **WhatsApp Voice**: Same number handles voice calls via WebRTC (Pipecat pipeline)
- **Session memory**: Text conversations persist for 24 hours
- **Chat API**: Same agent reachable via `POST /v1/chat` for web/API integration

## Prerequisites

1. **Meta Developer account** with a WhatsApp Business App
2. **WhatsApp Business phone number** (test number provided by Meta works for dev)
3. **OpenAI API key**
4. **Docker** (for Postgres + Redis)
5. **ngrok** (to expose local server to WhatsApp webhooks)

## Quick Start

### 1. Get WhatsApp credentials from Meta Developer Console

| Credential | Where to find | Env var |
|-----------|---------------|---------|
| **Access Token** | WhatsApp > API Setup > Generate token | `WHATSAPP_TOKEN` |
| **Phone Number ID** | WhatsApp > API Setup > "From" field (numeric ID) | `WHATSAPP_PHONE_NUMBER_ID` |
| **App Secret** | App Settings > Basic > App Secret (click Show, 32 hex chars) | `WHATSAPP_APP_SECRET` |
| **Verify Token** | You choose any string | `WHATSAPP_WEBHOOK_VERIFY_TOKEN` |

### 2. Configure environment

```bash
cp env.example .env
# Edit .env with your credentials:
#   OPENAI_API_KEY=sk-xxxxxxxx
#   WHATSAPP_TOKEN=EAAxxxxxxxx
#   WHATSAPP_PHONE_NUMBER_ID=1234567890
#   WHATSAPP_APP_SECRET=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
#   WHATSAPP_WEBHOOK_VERIFY_TOKEN=my-verify-token
```

### 3. Start infrastructure

```bash
make docker-up    # postgres + redis
make migrate      # create database tables
make run          # start server on http://localhost:8090
```

### 4. Expose with ngrok (new terminal)

```bash
ngrok http 8090
# Copy the https://xxxx.ngrok.io URL
```

### 5. Configure WhatsApp Webhook

In Meta Developer Console:

1. Go to **WhatsApp > Configuration**
2. Set **Callback URL** to: `https://xxxx.ngrok.io/webhooks/whatsapp`
3. Set **Verify token** to the same value as your `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
4. Click **Verify and save**
5. Subscribe to webhook fields: **messages** and **calls**

### 6. Run setup script

```bash
python examples/whatsapp/setup.py \
  --whatsapp-number "+1555XXXXXXX" \
  --whatsapp-phone-number-id "1234567890"
```

Use the **display phone number** from WhatsApp > API Setup (the "From" number in E.164 format)
and the **Phone Number ID** (numeric ID shown next to it).

This creates a project, agent, and binds the phone number with `whatsapp_enabled: true`.

### 7. Add your phone as a test recipient

> **Important**: In development mode, only registered test numbers can message your app.

1. Go to **WhatsApp > API Setup**
2. Under the **"To"** field, click **Manage phone number list**
3. Add your personal phone number and verify with the SMS code

### 8. Message your WhatsApp Business number!

Open WhatsApp on your phone, find the business number, and send a message.

| You send | What happens |
|----------|-------------|
| "Hi, what are your hours?" | AI replies with business hours |
| "I need to reschedule" | AI asks for details, continues conversation |
| (wait 24+ hours, message again) | New session starts with fresh context |
| (call the number) | AI answers via real-time voice |

## Architecture

### Text Message Flow
```
Customer messages WhatsApp Business number
  -> Meta POSTs to /webhooks/whatsapp (field: "messages")
  -> Validates X-Hub-Signature-256 (using App Secret)
  -> Resolves phone number -> Loads agent
  -> Creates or resumes 24h session
  -> Builds LLM message history from session
  -> Calls OpenAI chat completion
  -> Sends reply via WhatsApp Cloud API (POST /{phone_number_id}/messages)
```

### Voice Call Flow
```
Customer calls WhatsApp Business number
  -> Meta POSTs to /webhooks/whatsapp (field: "calls", event: "connect")
  -> Validates X-Hub-Signature-256
  -> Pipecat WhatsAppClient establishes WebRTC connection (SDP offer/answer)
  -> Pre-accept + Accept call via Cloud API
  -> Pipecat pipeline: VAD -> STT -> LLM -> TTS -> WebRTC transport (16kHz)
  -> On "terminate" event: cleanup resources
```

### Session Management
- Sessions auto-create on first message
- Resume within 24 hours (same customer + number pair)
- Expire after 24h inactivity -> new session on next message
- Background task cleans up expired sessions every 15 minutes

## Enabling Voice Calls

WhatsApp voice calling requires additional setup:

1. In Meta Developer Console, go to **WhatsApp > Configuration > Phone Numbers**
2. Select your number, go to the **Calls** tab
3. Enable **Allow voice calls**
4. Make sure the `calls` webhook field is subscribed

Voice calls use the same Pipecat pipeline as WebRTC browser calls (16kHz audio,
SmallWebRTCTransport) but with WhatsApp-specific SDP filtering for SHA-256
fingerprints.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `whatsapp_invalid_signature` (403) | Wrong App Secret | Copy from App Settings > Basic > App Secret (32 hex chars) |
| `Phone number not found` | Setup script not run or wrong number | Run `setup.py` with exact display number from API Setup |
| Messages not arriving | Webhook not configured | Check Callback URL and verify token in WhatsApp > Configuration |
| Can't message from phone | Dev mode restriction | Add your number as test recipient (step 7 above) |
| `WhatsApp not enabled on number` | Phone number binding missing flag | Re-run setup script or update via API with `whatsapp_enabled: true` |

## Going to Production

1. Complete **Meta Business Verification** (Settings > Business Verification)
2. Submit app for **App Review** with `whatsapp_manage` permission
3. Switch app to **Live Mode** (removes test number restriction)
4. Use a permanent webhook URL (not ngrok)

## Quick run

```bash
./run.sh --whatsapp-number +1555…
```

Reads `WHATSAPP_PHONE_NUMBER_ID` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
